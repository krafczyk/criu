#!/bin/env python
import argparse
import yaml
import os
import subprocess
import time
import tempfile
import shutil
import re
import stat
import signal
import atexit
import sys
import linecache

prev_line = None
def traceit(f, e, a):
	if e == "line":
		lineno = f.f_lineno
		fil = f.f_globals["__file__"]
		if fil.endswith("zdtm.py"):
			global prev_line
			line = linecache.getline(fil, lineno)
			if line == prev_line:
				print "        ..."
			else:
				prev_line = line
				print "+%4d: %s" % (lineno, line.rstrip())

	return traceit


# Descriptor for abstract test not in list
default_test={ }

# Root dir for ns and uns flavors. All tests
# sit in the same dir
zdtm_root = None

def clean_zdtm_root():
	global zdtm_root
	if zdtm_root:
		os.rmdir(zdtm_root)

def make_zdtm_root():
	global zdtm_root
	if not zdtm_root:
		zdtm_root = tempfile.mkdtemp("", "criu-root-", "/tmp")
		atexit.register(clean_zdtm_root)
	return zdtm_root

# Arch we run on
arch = os.uname()[4]

#
# Flavors
#  h -- host, test is run in the same set of namespaces as criu
#  ns -- namespaces, test is run in itw own set of namespaces
#  uns -- user namespace, the same as above plus user namespace
#

class host_flavor:
	def __init__(self, opts):
		self.name = "host"
		self.ns = False
		self.root = None

	def init(self, test_bin):
		pass

	def fini(self):
		pass

class ns_flavor:
	def __init__(self, opts):
		self.name = "ns"
		self.ns = True
		self.uns = False
		self.root = make_zdtm_root()

	def init(self, test_bin):
		print "Construct root for %s" % test_bin
		subprocess.check_call(["mount", "--make-private", "--bind", ".", self.root])

		if not os.access(self.root + "/.constructed", os.F_OK):
			for dir in ["/bin", "/etc", "/lib", "/lib64", "/dev", "/tmp"]:
				os.mkdir(self.root + dir)
				os.chmod(self.root + dir, 0777)

			os.mknod(self.root + "/dev/tty", stat.S_IFCHR, os.makedev(5, 0))
			os.chmod(self.root + "/dev/tty", 0666)
			os.mknod(self.root + "/.constructed", stat.S_IFREG | 0600)

		ldd = subprocess.Popen(["ldd", test_bin], stdout = subprocess.PIPE)
		xl = re.compile('^(linux-gate.so|linux-vdso(64)?.so|not a dynamic)')

		# This Mayakovsky-style code gets list of libraries a binary
		# needs minus vdso and gate .so-s
		libs = map(lambda x: x[1] == '=>' and x[2] or x[0],		\
				map(lambda x: x.split(),			\
					filter(lambda x: not xl.match(x),	\
						map(lambda x: x.strip(),	\
							filter(lambda x: x.startswith('\t'), ldd.stdout.readlines())))))
		ldd.wait()

		for lib in libs:
			tlib = self.root + lib
			if not os.access(tlib, os.F_OK):
				# Copying should be atomic as tests can be
				# run in parallel
				dst = tempfile.mktemp(".tso", "", self.root + os.path.dirname(lib))
				shutil.copy2(lib, dst)
				os.rename(dst, tlib)

	def fini(self):
		subprocess.check_call(["mount", "--make-private", self.root])
		subprocess.check_call(["umount", "-l", self.root])

class userns_flavor(ns_flavor):
	def __init__(self, opts):
		ns_flavor.__init__(self, opts)
		self.name = "userns"
		self.uns = True

flavors = { 'h': host_flavor, 'ns': ns_flavor, 'uns': userns_flavor }

#
# Helpers
#

def tail(path):
	p = subprocess.Popen(['tail', '-n1', path],
			stdout = subprocess.PIPE)
	return p.stdout.readline()

def rpidfile(path):
	return open(path).readline().strip()

def wait_pid_die(pid, who, tmo = 3):
	stime = 0.1
	while stime < tmo:
		try:
			os.kill(int(pid), 0)
		except: # Died
			break

		print "Wait for %s to die for %f" % (who, stime)
		time.sleep(stime)
		stime *= 2
	else:
		raise test_fail_exc("%s die" % who)

def test_flag(tdesc, flag):
	return flag in tdesc.get('flags', '').split()

#
# Exception thrown when something inside the test goes wrong,
# e.g. test doesn't start, criu returns with non zero code or
# test checks fail
#

class test_fail_exc:
	def __init__(self, step):
		self.step = step

#
# A test from zdtm/ directory.
#

class zdtm_test:
	def __init__(self, name, desc, flavor):
		self.__name = name
		self.__desc = desc
		self.__make_action('cleanout')
		self.__pid = 0
		self.__flavor = flavor

	@staticmethod
	def __zdtm_path(name, typ):
		return os.path.join("zdtm/live/", name + typ)

	def __getpath(self, typ = ''):
		return self.__zdtm_path(self.__name, typ)

	def __make_action(self, act, env = None, root = None):
		tpath = self.__getpath('.' + act)
		s_args = ['make', '--no-print-directory', \
			 	'-C', os.path.dirname(tpath), \
				      os.path.basename(tpath)]

		if env:
			env = dict(os.environ, **env)

		s = subprocess.Popen(s_args, env = env, cwd = root)
		s.wait()

	def __pidfile(self):
		if self.__flavor.ns:
			return self.__getpath('.init.pid')
		else:
			return self.__getpath('.pid')

	def __wait_task_die(self):
		wait_pid_die(int(self.__pid), self.__name)

	def start(self):
		env = {}
		self.__flavor.init(self.__getpath())

		print "Start test"

		env['ZDTM_THREAD_BOMB'] = "100"
		if not test_flag(self.__desc, 'suid'):
			env['ZDTM_UID'] = "18943"
			env['ZDTM_GID'] = "58467"
			env['ZDTM_GROUPS'] = "27495 48244"
		else:
			print "Test is SUID"

		if self.__flavor.ns:
			env['ZDTM_NEWNS'] = "1"
			env['ZDTM_PIDFILE'] = os.path.realpath(self.__getpath('.init.pid'))
			env['ZDTM_ROOT'] = self.__flavor.root

			if self.__flavor.uns:
				env['ZDTM_USERNS'] = "1"

		self.__make_action('pid', env, self.__flavor.root)

		try:
			os.kill(int(self.getpid()), 0)
		except:
			raise test_fail_exc("start")

	def kill(self, sig = signal.SIGKILL):
		if self.__pid:
			os.kill(int(self.__pid), sig)
			self.gone(sig == signal.SIGKILL)

		self.__flavor.fini()

	def stop(self):
		print "Stop test"
		self.kill(signal.SIGTERM)

		res = tail(self.__getpath('.out'))
		if not 'PASS' in res.split():
			raise test_fail_exc("result check")

	def getpid(self):
		if self.__pid == 0:
			self.__pid = rpidfile(self.__pidfile())

		return self.__pid

	def getname(self):
		return self.__name

	def getcropts(self):
		opts = self.__desc.get('opts', '').split() + ["--pidfile", os.path.realpath(self.__pidfile())]
		if self.__flavor.ns:
			opts += ["--root", self.__flavor.root]
		return opts

	def gone(self, force = True):
		self.__wait_task_die()
		self.__pid = 0
		if force or self.__flavor.ns:
			os.unlink(self.__pidfile())

	def print_output(self):
		print "Test output: " + "=" * 32
		print open(self.__getpath('.out')).read()
		print " <<< " + "=" * 32

	@staticmethod
	def checkskip(name):
		chs = zdtm_test.__zdtm_path(name, ".checkskip")
		if os.access(chs, os.X_OK):
			ch = subprocess.Popen([chs])
			return ch.wait() == 0 and False or True

		return False
			

#
# CRIU when launched using CLI
#

class criu_cli:
	def __init__(self, test, opts):
		self.__test = test
		self.__dump_path = "dump/" + test.getname() + "/" + test.getpid()
		self.__iter = 0
		os.makedirs(self.__dump_path)
		self.__page_server = (opts['page_server'] and True or False)

	def __ddir(self):
		return os.path.join(self.__dump_path, "%d" % self.__iter)

	@staticmethod
	def __criu(action, args):
		cr = subprocess.Popen(["../criu", action] + args)
		return cr.wait()

	def __criu_act(self, action, opts, log = None):
		if not log:
			log = action + ".log"

		s_args = ["-o", log, "-D", self.__ddir(), "-v4"] + opts

		print "Run CRIU: [" + " ".join(s_args) + "]"
		ret = self.__criu(action, s_args)
		if ret != 0:
			raise test_fail_exc("CRIU %s" % action)

	def __criu_cr(self, action, opts):
		self.__criu_act(action, opts = opts + self.__test.getcropts())

	def dump(self, action, opts = []):
		self.__iter += 1
		os.mkdir(self.__ddir())

		a_opts = ["-t", self.__test.getpid()]
		if self.__iter > 1:
			a_opts += ["--prev-images-dir", "../%d" % (self.__iter - 1), "--track-mem"]

		if self.__page_server:
			print "Adding page server"
			self.__criu_act("page-server", opts = [ "--port", "12345", \
					"--daemon", "--pidfile", "ps.pid"])
			a_opts += ["--page-server", "--address", "127.0.0.1", "--port", "12345"]

		self.__criu_cr(action, opts = a_opts + opts)

		if self.__page_server:
			wait_pid_die(int(rpidfile(self.__ddir() + "/ps.pid")), "page server")

	def restore(self):
		self.__criu_cr("restore", opts = ["--restore-detached"])

	@staticmethod
	def check(feature):
		return criu_cli.__criu("check", ["-v0", "--feature", feature]) == 0

#
# Main testing entity -- dump (probably with pre-dumps) and restore
#

def cr(test, opts):
	if opts['nocr']:
		return

	cr_api = criu_cli(test, opts)

	for i in xrange(0, int(opts['iters'] or 1)):
		for p in xrange(0, int(opts['pre'] or 0)):
			cr_api.dump("pre-dump")

		if opts['norst']:
			cr_api.dump("dump", opts = ["--leave-running"])
		else:
			cr_api.dump("dump")
			test.gone()
			cr_api.restore()

# Additional checks that can be done outside of test process

def get_maps(test):
	maps = [[0,0]]
	last = 0
	for mp in open("/proc/%s/maps" % test.getpid()).readlines():
		m = map(lambda x: int('0x' + x, 0), mp.split()[0].split('-'))
		if maps[last][1] == m[0]:
			maps[last][1] = m[1]
		else:
			maps.append(m)
			last += 1
	maps.pop(0)
	return maps

def get_fds(test):
	return map(lambda x: int(x), os.listdir("/proc/%s/fdinfo" % test.getpid()))

def cmp_lists(m1, m2):
	return filter(lambda x: x[0] != x[1], zip(m1, m2))

def get_visible_state(test):
	fds = get_fds(test)
	maps = get_maps(test)
	return (fds, maps)

def check_visible_state(test, state):
	new = get_visible_state(test)
	if cmp_lists(new[0], state[0]):
		raise test_fail_exc("fds compare")
	if cmp_lists(new[1], state[1]):
		raise test_fail_exc("maps compare")

def do_run_test(tname, tdesc, flavs, opts):
	print "Run %s in %s" % (tname, flavs)

	for f in flavs:
		flav = flavors[f](opts)
		t = zdtm_test(tname, tdesc, flav)

		try:
			t.start()
			s = get_visible_state(t)
			cr(t, opts)
			check_visible_state(t, s)
			t.stop()
		except test_fail_exc as e:
			t.print_output()
			t.kill()
			print "Test %s FAIL at %s" % (tname, e.step)
			# This exit does two things -- exits from subprocess and
			# aborts the main script execution on the 1st error met
			sys.exit(1)
		else:
			print "Test %s PASS" % tname

class launcher:
	def __init__(self, opts):
		self.__opts = opts
		self.__max = int(opts['parallel'] or 0)
		self.__subs = {}
		self.__fail = False

	def run_test(self, name, desc, flavor):
		if self.__max == 0:
			do_run_test(name, desc, flavor, self.__opts)
			return

		if len(self.__subs) >= self.__max:
			self.wait()
			if self.__fail:
				raise test_fail_exc('')

		nd = ('nocr', 'norst', 'pre', 'iters', 'page_server')
		arg = repr((name, desc, flavor, { d: self.__opts[d] for d in nd }))
		log = name.replace('/', '_') + ".log"
		sub = subprocess.Popen(["zdtm_ct", "zdtm.py"], env = { 'ZDTM_CT_TEST_INFO': arg }, \
				stdout = open(log, "w"), stderr = subprocess.STDOUT)
		self.__subs[sub.pid] = { 'sub': sub, 'log': log }

	def __wait_one(self, flags):
		pid, status = os.waitpid(0, flags)
		if pid != 0:
			sub = self.__subs.pop(pid)
			if status != 0:
				self.__fail = True

			print open(sub['log']).read()
			os.unlink(sub['log'])
			return True

		return False

	def wait(self):
		self.__wait_one(0)
		while self.__subs:
			if not self.__wait_one(os.WNOHANG):
				break

	def finish(self):
		while self.__subs:
			self.__wait_one(0)
		if self.__fail:
			sys.exit(1)

def run_tests(opts, tlist):
	excl = None
	features = {}

	if opts['all']:
		torun = tlist
	elif opts['test']:
		torun = opts['test']
	else:
		print "Specify test with -t <name> or -a"
		return

	if opts['exclude']:
		excl = re.compile(".*(" + "|".join(opts['exclude']) + ")")
		print "Compiled exclusion list"

	l = launcher(opts)
	try:
		for t in torun:
			global arch

			if excl and excl.match(t):
				print "Skipping %s (exclude)" % t
				continue

			tdesc = tlist.get(t, default_test) or default_test
			if tdesc.get('arch', arch) != arch:
				print "Skipping %s (arch %s)" % (t, tdesc['arch'])
				continue

			feat = tdesc.get('feature', None)
			if feat:
				if not features.has_key(feat):
					print "Checking feature %s" % feat
					features[feat] = criu_cli.check(feat)

				if not features[feat]:
					print "Skipping %s (no %s feature)" % (t, feat)
					continue

			if zdtm_test.checkskip(t):
				print "Skipping %s (self)" % t
				continue

			test_flavs = tdesc.get('flavor', 'h ns uns').split()
			opts_flavs = (opts['flavor'] or 'h,ns,uns').split(',')
			run_flavs = set(test_flavs) & set(opts_flavs)

			if run_flavs:
				l.run_test(t, tdesc, run_flavs)
	finally:
		l.finish()

def list_tests(opts, tlist):
	for t in tlist:
		print t

#
# main() starts here
#

if os.environ.has_key('ZDTM_CT_TEST_INFO'):
	tinfo = eval(os.environ['ZDTM_CT_TEST_INFO'])
	do_run_test(tinfo[0], tinfo[1], tinfo[2], tinfo[3])
	sys.exit(0)

p = argparse.ArgumentParser("ZDTM test suite")
p.add_argument("--debug", help = "Print what's being executed", action = 'store_true')

sp = p.add_subparsers(help = "Use --help for list of actions")

rp = sp.add_parser("run", help = "Run test(s)")
rp.set_defaults(action = run_tests)
rp.add_argument("-a", "--all", action = 'store_true')
rp.add_argument("-t", "--test", help = "Test name", action = 'append')
rp.add_argument("-f", "--flavor", help = "Flavor to run")
rp.add_argument("-x", "--exclude", help = "Exclude tests from --all run", action = 'append')

rp.add_argument("--pre", help = "Do some pre-dumps before dump")
rp.add_argument("--nocr", help = "Do not CR anything, just check test works", action = 'store_true')
rp.add_argument("--norst", help = "Don't restore tasks, leave them running after dump", action = 'store_true')
rp.add_argument("--iters", help = "Do CR cycle several times before check")

rp.add_argument("--page-server", help = "Use page server dump", action = 'store_true')
rp.add_argument("-p", "--parallel", help = "Run test in parallel")

lp = sp.add_parser("list", help = "List tests")
lp.set_defaults(action = list_tests)

opts = vars(p.parse_args())
tlist = yaml.load(open("zdtm.list"))

if opts['debug']:
	sys.settrace(traceit)

opts['action'](opts, tlist)
