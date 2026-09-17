"""Shared, terminal-cell-aware columns for settings option lists."""

from collections.abc import Iterable
from dataclasses import dataclass

from textual.content import Content
from textual.visual import Visual
from textual.widgets import OptionList

from .bidi import visual_text


def label_width(labels: Iterable[str]) -> int:
    return max((Content(visual_text(label)).cell_length for label in labels), default=0)


def setting_row(
    options: OptionList,
    label: str,
    value_markup: str,
    longest_label: int,
    *,
    indent: int = 2,
) -> "SettingRow":
    """Create a row that wraps at paint time, once its actual width is known.

    Mounting a new screen gives its OptionList a zero-width region. Eagerly
    wrapping then shows a column of single letters for the first frame; rebuilding
    after refresh only fixes the *next* frame. A Visual also reflows on resize
    without clearing the list or disturbing its highlight and scroll position.
    """
    return SettingRow(
        Content(visual_text(label)), Content.from_markup(value_markup),
        longest_label, indent, options.styles.scrollbar_size_vertical,
    )


@dataclass
class SettingRow(Visual):
    label: Content
    value: Content
    longest_label: int
    indent: int = 2
    gutter: int = 1

    def content(self, width: int) -> Content:
        # OptionList has already subtracted option padding from the render width.
        width = max(1, width - self.gutter)
        if width < 5:
            return Content("\n").join([*self.label.wrap(width), *self.value.wrap(width)])
        indent = min(self.indent, max(0, width - 5))
        usable = width - indent - 2
        left_width = max(1, min(self.longest_label, usable // 2))
        right_width = max(1, usable - left_width)
        left = self.label.wrap(left_width)
        right = self.value.wrap(right_width)
        lines: list[Content] = []
        for index in range(max(len(left), len(right))):
            name = left[index] if index < len(left) else Content("")
            value = right[index] if index < len(right) else Content("")
            lines.append(
                Content(" " * indent) + name
                + Content(" " * (left_width - name.cell_length + 2)) + value
            )
        return Content("\n").join(lines)

    def render_strips(self, width, height, style, options):
        return self.content(width).render_strips(width, height, style, options)

    def get_height(self, rules, width):
        return self.content(width).get_height(rules, width)

    def get_optimal_width(self, rules, container_width):
        return container_width
