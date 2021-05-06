import logging

import sublime
import sublime_plugin

from sublime_lib import ResourcePath

from .lib import syntax_paths
from .lib import inhibit_word_completions

__all__ = (
    'PackagedevEditThemeCommand',
    'ThemeCompletionsListener',
)

THEME_TEMPLATE = """\
{
  // http://www.sublimetext.com/docs/3/themes.html
  "variables": {
    // "font_face": "system",
  },
  "rules": [
  ],
}""".replace("  ", "\t")

KIND_THEME = (sublime.KIND_ID_VARIABLE, "t", "Theme")

# Copied from 'Default/ui.py'
DEFAULT_THEME = 'Default.sublime-theme'

logger = logging.getLogger(__name__)


class PackagedevEditThemeCommand(sublime_plugin.WindowCommand):

    """Like syntax-specific settings but for the currently used theme."""

    def run(self):
        settings = sublime.load_settings('Preferences.sublime-settings')
        theme_name = settings.get('theme', DEFAULT_THEME)

        if theme_name != 'auto':
            self.open_theme(theme_name)
        else:
            choices = [
                sublime.QuickPanelItem(
                    setting,
                    details=settings.get(setting, DEFAULT_THEME),
                    kind=KIND_THEME,
                )
                for setting in ('dark_theme', 'light_theme')
            ]

            current_os_mode = sublime.ui_info()['system']['style']
            selected_index = -1
            for idx, choice in enumerate(choices):
                if choice.trigger.startswith(current_os_mode):
                    choice.annotation = 'Active'
                    selected_index = idx

            def on_done(i):
                if i >= 0:
                    self.open_theme(choices[i].details)

            self.window.show_quick_panel(
                choices,
                on_done,
                selected_index=selected_index,
                placeholder="Choose a theme to edit ..."
            )

    def open_theme(self, theme_name):
        theme_path = ResourcePath(sublime.find_resources(theme_name)[0])
        self.window.run_command(
            'edit_settings',
            {
                'base_file': "/".join(("${packages}",) + theme_path.parts[1:]),
                'user_file': "${packages}/User/" + theme_path.name,
                'default': THEME_TEMPLATE,
            },
        )


class ThemeCompletionsListener(sublime_plugin.ViewEventListener):

    """Provide completions for themes.

    Offer names of theme files to extend.
    Other completions are shared with color schemes
    and implemented in color_scheme_dev.py.
    """

    @classmethod
    def applies_to_primary_view_only(cls):
        return False

    @classmethod
    def is_applicable(cls, settings):
        return settings.get('syntax') in (syntax_paths.COLOR_SCHEME, syntax_paths.THEME)

    @inhibit_word_completions
    def on_query_completions(self, prefix, locations):
        if len(locations) != 1:
            return None
        point = locations[0]

        if self.view.match_selector(point, "meta.extends.sublime-theme"):
            return self.extends_completions()

    def extends_completions(self):
        resources = sublime.find_resources("*.sublime-theme")
        resources += sublime.find_resources("*.hidden-theme")
        names = {res.rsplit("/", 1)[-1] for res in resources}

        if self.view.file_name():
            names -= {ResourcePath.from_file_path(self.view.file_name()).name}

        sorted_names = sorted(names)
        logger.debug("Found %d themes to complete: %r", len(names), sorted_names)
        return [
            sublime.CompletionItem(
                trigger=name,
                completion=name,
                kind=KIND_THEME,
            )
            for name in sorted_names
        ]
