from __future__ import print_function

import os
import platform
import signal
import threading
import traceback
from sexpdata import dumps, loads, Symbol, Quoted

from euslime.bridge import AbortEvaluation
from euslime.bridge import EuslispError
from euslime.bridge import EuslispProcess
from euslime.bridge import EuslispResult
from euslime.logger import get_logger

log = get_logger(__name__)


def findp(s, l):
    assert isinstance(l, list)
    # Reverse search list
    for e in l[::-1]:
        if e == s:
            return l
        elif isinstance(e, list):
            res = findp(s, e)
            if res:
                return res
    return list()


def current_scope(sexp):
    marker = Symbol(u'swank::%cursor-marker%')
    scope = findp(marker, sexp)
    cursor = -1
    for i, e in enumerate(scope):
        if marker == e:
            cursor = i
    return scope, cursor


def qstr(s):
    # double escape characters for string formatting
    return s.encode('utf-8').encode('string_escape').replace('"', '\\"')


def dumps_lisp(s):
    return dumps(s, true_as='lisp:t', false_as='lisp:nil', none_as='lisp:nil')


class DebuggerHandler(object):
    restarts = [
        ["QUIT", "Quit to the SLIME top level"],
        ["CONTINUE", "Ignore the error and continue in the same stack level"],
        ["RESTART", "Restart euslisp process"]
    ]

    def __init__(self, id, error):
        self.id = id
        if isinstance(error, EuslispError) and error.fatal:
            self.restarts = self.restarts[2:]  # No QUIT & CONTINUE
        else:
            self.restarts = self.restarts
        self.restarts_dict = {}
        for num, item in enumerate([x[0] for x in self.restarts]):
            self.restarts_dict[item] = num
        if isinstance(error, EuslispError):
            self.message = error.message
            self.stack = error.stack
        elif isinstance(error, Exception):
            self.message = '{}: {}'.format(type(error).__name__, error)
            self.stack = []
        else:
            self.message = error
            self.stack = []


class EuslimeHandler(object):
    def __init__(self, *args, **kwargs):
        self.euslisp = EuslispProcess(*args, **kwargs)
        self.close_request = threading.Event()
        self.thread_local = threading.local()
        self.euslisp.start()
        self.package = None
        self.debugger = []

    def restart_euslisp_process(self):
        program = self.euslisp.program
        init_file = self.euslisp.init_file
        on_output = self.euslisp.on_output
        color = self.euslisp.color
        self.euslisp.stop()
        self.euslisp = EuslispProcess(program, init_file, on_output=on_output, color=color)
        self.euslisp.start()
        self.euslisp.accumulate_output.clear()

    def maybe_new_prompt(self):
        new_prompt = self.euslisp.exec_internal('(slime::slime-prompt)', force_repl_socket=True)
        if new_prompt:
            yield [Symbol(":new-package")] + new_prompt

    def arglist(self, func, cursor=None, form=None):
        if not isinstance(func, unicode) and not isinstance(func, str):
            log.debug("Expected string at: %s" % func)
            return None
        cmd = '(slime::autodoc "{0}" {1} (lisp:quote {2}))'.format(
            qstr(func), dumps_lisp(cursor), dumps_lisp(form))
        result = self.euslisp.exec_internal(cmd, force_repl_socket=True)
        if isinstance(result, str):
            return [result, False]
        elif result:
            return [dumps(result), True]
        return None

    def _emacs_return_string(self, process, count, msg):
        self.euslisp.input(msg)
        yield [Symbol(":read-string"), 0, 1]

    def _emacs_interrupt(self, process):
        self.euslisp.process.send_signal(signal.SIGINT)
        if process == Symbol(':repl-thread'):
            # Interrupted from the top level
            lock = self.euslisp.euslime_connection_lock
            log.debug('Acquiring lock: %s' % lock)
            lock.acquire()
            try:
                for val in self.maybe_new_prompt():
                    yield val
                lock.release()
            except Exception as e:
                if lock.locked():
                    lock.release()

    def swank_connection_info(self):
        version = self.euslisp.exec_internal('(slime::implementation-version)')
        name = self.euslisp.exec_internal('(lisp:pathname-name lisp:*program-name*)')
        res = {
            'pid': os.getpid(),
            'style': False,
            'encoding': {
                'coding-systems': ['utf-8-unix', 'iso-latin-1-unix'],
            },
            'lisp-implementation': {
                'type': name,
                'name': name,
                'version': version,
                'program': False,
            },
            'machine': {
                'type': platform.machine().upper(),
                'version': platform.machine().upper(),
            },
            'package': {
                'name': 'USER',
                'prompt': name,
            },
            'version': "2.20",  # swank version
        }
        yield EuslispResult(res)

    def swank_create_repl(self, sexp):
        res = self.euslisp.exec_internal('(slime::slime-prompt)')
        self.euslisp.accumulate_output.clear()
        yield EuslispResult(res)

    def swank_repl_create_repl(self, *sexp):
        return self.swank_create_repl(sexp)

    def swank_buffer_first_change(self, filename):
        yield EuslispResult(False)

    def swank_eval(self, sexp):
        lock = self.euslisp.euslime_connection_lock
        log.debug('Acquiring lock: %s' % lock)
        lock.acquire()
        yield [Symbol(":read-string"), 0, 1]
        try:
            gen = list(self.euslisp.eval(sexp))
            yield [Symbol(":read-aborted"), 0, 1]
            for val in gen:
                yield val
            for val in self.maybe_new_prompt():
                yield val
            yield EuslispResult(None)
            lock.release()
        except AbortEvaluation as e:
            if lock.locked():
                lock.release()
            log.info('Aborting evaluation...')
            yield [Symbol(":read-aborted"), 0, 1]
            for val in self.maybe_new_prompt():
                yield val
            if e.message:
                yield EuslispResult(e.message, response_type='abort')
            else:
                yield EuslispResult(None)
        except Exception as e:
            if lock.locked():
                lock.release()
            yield [Symbol(":read-aborted"), 0, 1]
            raise e

    def swank_interactive_eval(self, sexp):
        return self.swank_eval(sexp)

    def swank_interactive_eval_region(self, sexp):
        return self.swank_eval(sexp)

    def swank_repl_listener_eval(self, sexp):
        if self.debugger:
            return self.swank_throw_to_toplevel()
        return self.swank_eval(sexp)

    def swank_pprint_eval(self, sexp):
        return self.swank_eval(sexp)

    def swank_autodoc(self, sexp, _=None, line_width=None):
        """
(:emacs-rex
 (swank:autodoc
  '("ql:quickload" "" swank::%cursor-marker%)
  :print-right-margin 102)
 "COMMON-LISP-USER" :repl-thread 19)
(:return
 (:ok
  ("(quickload ===> systems <=== &key
     (verbose quicklisp-client:*quickload-verbose*) silent\n
     (prompt quicklisp-client:*quickload-prompt*) explain &allow-other-keys)"
   t))
 19)
        """
        try:
            # unquote
            if isinstance(sexp, Quoted):
                # For instance in emacs 27
                sexp = sexp.value()
            else:
                sexp = sexp[1]  # [Symbol('quote'), [ ... ]]
            scope, cursor = current_scope(sexp)
            log.debug("scope: %s, cursor: %s" % (scope, cursor))
            assert cursor > 0
            func = scope[0]
            scope = scope[:-1]  # remove marker
            result = self.arglist(func, cursor, scope)
            if result:
                yield EuslispResult(result)
            else:
                yield EuslispResult([Symbol(":not-available"), True])
        except Exception:
            log.error(traceback.format_exc())
            yield EuslispResult([Symbol(":not-available"), True])

    def swank_operator_arglist(self, func, pkg):
        #  (swank:operator-arglist "format" "USER")
        result = self.arglist(func)
        if result:
            result = result[0]
        yield EuslispResult(result)

    def swank_completions(self, start, pkg):
        if start and start[0] == ':':
            return self.swank_completions_for_keyword(start, None)
        else:
            return self.swank_simple_completions(start, pkg)
        # return self.swank_fuzzy_completions(start, pkg, ':limit', 300,
        #                                     ':time-limit-in-msec', 1500)

    def swank_simple_completions(self, start, pkg):
        # (swank:simple-completions "vector-" (quote "USER"))
        cmd = '(slime::slime-find-symbol "{0}")'.format(qstr(start))
        yield EuslispResult(self.euslisp.exec_internal(cmd))

    def swank_fuzzy_completions(self, start, pkg, *args):
        # Unsupported. Use swank_simple_completions instead
        yield EuslispResult([None, None])

    def swank_completions_for_keyword(self, start, sexp):
        if sexp:
            sexp = sexp[1]  # unquote
            scope, _ = current_scope(sexp)
            if scope:
                scope = scope[:-1]  # remove marker
                if scope[-1] in ['', u'']:  # remove null string
                    scope = scope[:-1]

        else:
            scope = None
        cmd = '(slime::slime-find-keyword "{0}" (lisp:quote {1}))'.format(
            qstr(start), dumps_lisp(scope))
        yield EuslispResult(self.euslisp.exec_internal(cmd))

    def swank_completions_for_character(self, start):
        cmd = '(slime::slime-find-character "{0}")'.format(qstr(start))
        yield EuslispResult(self.euslisp.exec_internal(cmd))

    def swank_complete_form(self, *args):
        # (swank:complete-form
        #    (quote ("float-vector" swank::%cursor-marker%))
        return

    def swank_quit_lisp(self, *args):
        self.euslisp.stop()
        self.close_request.set()

    def swank_backtrace(self, start, end):
        res = self.euslisp.get_callstack(end)
        yield EuslispResult(res[start:])

    def swank_throw_to_toplevel(self):
        lvl = len(self.debugger)
        return self.swank_invoke_nth_restart_for_emacs(lvl, 0)

    def swank_invoke_nth_restart_for_emacs(self, level, num):
        deb = self.debugger.pop(level - 1)
        res_dict = deb.restarts_dict

        def check_key(key):
            return key in res_dict and num == res_dict[key]

        if check_key('RESTART'):
            self.debugger = []
            self.restart_euslisp_process()
        elif check_key('CONTINUE'):
            pass
        elif check_key('QUIT'):
            self.debugger = []
            self.euslisp.reset()
        else:
            log.error("Restart not found!")

        msg = deb.message.split(self.euslisp.delim)[0]
        msg = repr(msg.rsplit(' in ', 1)[0])
        yield [Symbol(':debug-return'), 0, level, Symbol('nil')]
        yield [Symbol(':return'), {'abort': 'NIL'}, self.thread_local.comm_id]
        for val in self.maybe_new_prompt():
            yield val
        yield [Symbol(':return'), {'abort': msg}, deb.id]

    def swank_swank_require(self, *sexp):
        return

    def swank_init_presentations(self, *sexp):
        return

    def swank_compile_string_for_emacs(self, cmd_str, *args):
        # (sexp buffer-name (:position 1) (:line 1) () ())
        # FIXME: This does not compile actually, just eval instead.
        # Although compiling it would have effect on the Call Stack
        cmd_str = "(lisp:progn " + cmd_str + ")"
        messages = []
        sexp = loads(cmd_str, nil=None)
        for exp in sexp[1:]:
            if len(exp) > 2:
                messages.append(dumps(exp[:2] + [None], none_as='...'))
            else:
                messages.append(dumps(exp))
        lock = self.euslisp.euslime_connection_lock
        log.debug('Acquiring lock: %s' % lock)
        lock.acquire()
        yield [Symbol(":read-string"), 0, 1]
        try:
            list(self.euslisp.eval(cmd_str))
            yield [Symbol(":read-aborted"), 0, 1]
            for msg in messages:
                yield [Symbol(":write-string"), "; Loaded {}\n".format(msg)]

            errors = []
            seconds = 0.01
            yield EuslispResult([Symbol(":compilation-result"), errors, True,
                                 seconds, None, None])
            lock.release()
        except AbortEvaluation as e:
            if lock.locked():
                lock.release()
            log.info('Aborting evaluation...')
            yield [Symbol(":read-aborted"), 0, 1]
            if e.message:
                yield EuslispResult(e.message, response_type='abort')
            else:
                yield EuslispResult(None)
        except Exception as e:
            if lock.locked():
                lock.release()
            yield [Symbol(":read-aborted"), 0, 1]
            raise e

    def swank_compile_notes_for_emacs(self, *args):
        return self.swank_compile_string_for_emacs(*args)

    def swank_compile_file_for_emacs(self, *args):
        return self.swank_compile_file_if_needed(*args)

    def swank_compile_file_if_needed(self, filename, loadp):
        # FIXME: This returns without checking/compiling the file
        errors = []
        seconds = 0.01
        yield EuslispResult([Symbol(":compilation-result"), errors, True,
                             seconds, loadp, filename])

    def swank_load_file(self, filename):
        lock = self.euslisp.euslime_connection_lock
        log.debug('Acquiring lock: %s' % lock)
        lock.acquire()
        yield [Symbol(":write-string"), "Loading file: %s ...\n" % filename]
        try:
            res = self.euslisp.exec_internal('(lisp:load "{0}")'.format(qstr(filename)),
                                             force_repl_socket=True)
            yield [Symbol(":write-string"), "Loaded.\n"]
            yield EuslispResult(res)
            lock.release()
        except AbortEvaluation as e:
            if lock.locked():
                lock.release()
            log.info('Aborting evaluation...')
            # Force print the message, which is by default only displayed on the minibuffer
            if e.message:
                yield [Symbol(":write-string"),
                       "; Evaluation aborted on {}\n".format(e.message),
                       Symbol(":repl-result")]
            yield EuslispResult(None)
        except Exception as e:
            if lock.locked():
                lock.release()
            raise e

    def swank_inspect_current_condition(self):
        # (swank:inspect-current-condition)
        return

    def swank_sldb_abort(self, *args):
        return

    def swank_sldb_out(self, *args):
        return

    def swank_frame_locals_and_catch_tags(self, *args):
        return

    def swank_find_definitions_for_emacs(self, keyword):
        return

    def swank_describe_symbol(self, sym):
        cmd = '(slime::slime-describe-symbol "{0}")'.format(
            qstr(sym.strip()))
        yield EuslispResult(self.euslisp.exec_internal(cmd))

    def swank_describe_function(self, func):
        return self.swank_describe_symbol(func)

    def swank_describe_definition_for_emacs(self, name, type):
        return self.swank_describe_symbol(name)

    def swank_swank_expand_1(self, form):
        cmd = '(slime::slime-macroexpand (lisp:quote {0}))'.format(form)
        yield EuslispResult(self.euslisp.exec_internal(cmd))

    def swank_list_all_package_names(self, nicknames=None):
        cmd = '(slime::slime-all-packages {0})'.format(
            dumps_lisp(nicknames))
        yield EuslispResult(self.euslisp.exec_internal(cmd))

    def swank_apropos_list_for_emacs(self, key, external_only=None,
                                     case_sensitive=None, package=None):
        # ignore 'external_only' and 'case_sensitive' arguments
        package = package[-1]  # unquote
        cmd = '(slime::slime-apropos-list "{0}" {1})'.format(
            qstr(key), dumps_lisp(package))
        yield EuslispResult(self.euslisp.exec_internal(cmd))

    def swank_repl_clear_repl_variables(self):
        cmd = self.euslisp.exec_internal('(slime::clear-repl-variables)',
                                         force_repl_socket=True)
        yield EuslispResult(cmd)

    def swank_clear_repl_results(self):
        return

    def swank_set_package(self, name):
        cmd = '(slime::set-package "{0}")'.format(qstr(name))
        res = self.euslisp.exec_internal(cmd, force_repl_socket=True)
        yield EuslispResult(res)

    def swank_default_directory(self):
        cmd = self.euslisp.exec_internal('(lisp:pwd)')
        yield EuslispResult(cmd)

    def swank_set_default_directory(self, dir):
        cmd = '(lisp:progn (lisp:cd "{0}") (lisp:pwd))'.format(qstr(dir))
        yield EuslispResult(self.euslisp.exec_internal(cmd))


if __name__ == '__main__':
    h = EuslimeHandler()
    print(dumps(h.swank_connection_info().next()))
