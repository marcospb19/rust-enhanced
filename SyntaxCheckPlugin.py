import sublime
import sublime_plugin
import os
from .rust import (messages, rust_proc, rust_thread, util, target_detect,
                   cargo_settings, semver)
from pprint import pprint


"""On-save syntax checking.

This contains the code for displaying message phantoms for errors/warnings
whenever you save a Rust file.
"""

# Notes:
# - -Zno-trans has been deprecated, and will only be available with the
#   nightly compiler starting with rust 1.19.  Using "cargo check" is the
#   preferred alternative, though it currently has some limitations. See:
#
#       - Unstable flags removed:
#         https://github.com/rust-lang/rust/issues/31847
#
#       - Cargo check added in rust 1.16:
#         https://github.com/rust-lang/cargo/pull/3296 (based on original
#         "cargo check" addon https://github.com/rsolomo/cargo-check/)
#
#       - RLS was recently released
#         (https://github.com/rust-lang-nursery/rls).  It's unclear to me if
#         this will perform full-linting that could replace this or not.
#
#       - "cargo check" ignores #[test]:
#         https://github.com/rust-lang/cargo/issues/3431
#
#       - "cargo check" will not re-issue errors/warnings if nothing has
#         changed.  This generally should not be a problem since on-save
#         syntax only runs after the file has been saved which updates the
#         timestamp, but it's something to be careful about.  See:
#         https://github.com/rust-lang/cargo/issues/3624
#
# - -Zno-trans prevents some warnings and errors from being generated. For
#   example, see const-err.rs.  "cargo check" does not have this problem.
#   Other issues:
#       - Errors generated by compiling an extern crate do not not output as
#         json.


class RustSyntaxCheckEvent(sublime_plugin.EventListener):

    # Beware: This gets called multiple times if the same buffer is opened in
    # multiple views (with the same view passed in each time).  See:
    # https://github.com/SublimeTextIssues/Core/issues/289
    def on_post_save(self, view):
        # Are we in rust scope and is it switched on?
        # We use phantoms which were added in 3118
        if int(sublime.version()) < 3118:
            return

        enabled = util.get_setting('rust_syntax_checking', True)
        if enabled and util.active_view_is_rust(view=view):
            t = RustSyntaxCheckThread(view)
            t.start()
        elif not enabled:
            # If the user has switched OFF the plugin, remove any phantom
            # lines.
            messages.clear_messages(view.window())


class RustSyntaxCheckThread(rust_thread.RustThread, rust_proc.ProcListener):

    # Thread name.
    name = 'Syntax Check'
    # The Sublime view that triggered the check.
    view = None
    # Absolute path to the view that triggered the check.
    triggered_file_name = None
    # Directory where cargo will be run.
    cwd = None
    # Base path for relative paths in messages.
    msg_rel_path = None
    # This flag is used to terminate early. In situations where we can't
    # auto-detect the appropriate Cargo target, we compile multiple targets.
    # If we receive any messages for the current view, we might as well stop.
    # Otherwise, you risk displaying duplicate messages for shared modules.
    this_view_found = False
    # The path to the top-level Cargo target filename (like main.rs or
    # lib.rs).
    current_target_src = None

    def __init__(self, view):
        self.view = view
        super(RustSyntaxCheckThread, self).__init__(view.window())

    def run(self):
        self.triggered_file_name = os.path.abspath(self.view.file_name())
        self.cwd = util.find_cargo_manifest(self.triggered_file_name)
        if self.cwd is None:
            # A manifest is required.
            print('Rust Enhanced skipping on-save syntax check.')
            print('Failed to find Cargo.toml from %r' % self.triggered_file_name)
            print('A Cargo.toml manifest is required.')
            return

        self.view.set_status('rust-check', 'Rust syntax check running...')
        self.this_view_found = False
        try:
            messages.clear_messages(self.window)
            try:
                self.get_rustc_messages()
            except rust_proc.ProcessTerminatedError:
                return
            messages.messages_finished(self.window)
        finally:
            self.view.erase_status('rust-check')

    def get_rustc_messages(self):
        """Top-level entry point for generating messages for the given
        filename.

        :raises rust_proc.ProcessTerminatedError: Check was canceled.
        """
        method = util.get_setting('rust_syntax_checking_method', 'check')
        settings = cargo_settings.CargoSettings(self.window)
        settings.load()
        command_info = cargo_settings.CARGO_COMMANDS[method]

        if method == 'clippy':
            # Clippy does not support cargo target filters, must be run for
            # all targets.
            cmd = settings.get_command(method, command_info, self.cwd,
                self.cwd, force_json=True)
            self.msg_rel_path = cmd['msg_rel_path']
            p = rust_proc.RustProc()
            p.run(self.window, cmd['command'], self.cwd, self, env=cmd['env'])
            p.wait()
            return

        # "no-trans" or "check" methods.
        td = target_detect.TargetDetector(self.window)
        targets = td.determine_targets(self.triggered_file_name)
        for (target_src, target_args) in targets:
            cmd = settings.get_command(method, command_info, self.cwd, self.cwd,
                initial_settings={'target': ' '.join(target_args)},
                force_json=True)
            self.msg_rel_path = cmd['msg_rel_path']
            if method == 'no-trans':
                cmd['command'].extend(['--', '-Zno-trans', '-Zunstable-options'])
                if (util.get_setting('rust_syntax_checking_include_tests', True) and
                    not ('--test' in target_args or '--bench' in target_args)):
                    # Including the test harness has a few drawbacks.
                    # missing_docs lint is disabled (see
                    # https://github.com/rust-lang/sublime-rust/issues/156)
                    # It also disables the "main function not found" error for
                    # binaries.
                    cmd['command'].append('--test')
            elif method == 'check':
                if (util.get_setting('rust_syntax_checking_include_tests', True) and
                    semver.match(cmd['rustc_version'], '>=1.23.0')):
                    cmd['command'].append('--profile=test')
            p = rust_proc.RustProc()
            self.current_target_src = target_src
            p.run(self.window, cmd['command'], self.cwd, self, env=cmd['env'])
            p.wait()
            if self.this_view_found:
                break

    #########################################################################
    # ProcListner methods
    #########################################################################

    def on_begin(self, proc):
        pass

    def on_data(self, proc, data):
        # Debugging on-save checking problems requires viewing output here,
        # but it is difficult to segregate useful messages (like "thread
        # 'main' panicked") from all the other output.  Perhaps make a debug
        # print setting?
        pass

    def on_error(self, proc, message):
        print('Rust Error: %s' % message)

    def on_json(self, proc, obj):
        messages.add_rust_messages(self.window, self.msg_rel_path, obj,
                                   self.current_target_src, msg_cb=None)
        if messages.has_message_for_path(self.window,
                                         self.triggered_file_name):
            self.this_view_found = True

    def on_finished(self, proc, rc):
        pass

    def on_terminated(self, proc):
        pass
