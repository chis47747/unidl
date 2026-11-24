"""Shared, terminal-cell-aware columns for settings option lists."""

from collections.abc import Iterable
from dataclasses import dataclass

from textual.content import Content
from textual.strip import Strip
from textual.visual import Visual
from textual.widgets import OptionList

from ..core.i18n import is_rtl_locale
from .bidi import shape_strip, visual_text


def label_width(labels: Iterable[str]) -> int:
    return max(
        (
            max(Content(str(label or "")).cell_length, Content(visual_text(label)).cell_length)
            for label in labels
        ),
        default=0,
    )


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
        Content(str(label or "")),
        Content.from_markup(value_markup),
        longest_label,
        indent,
        options.styles.scrollbar_size_vertical,
    )


@dataclass
class SettingRow(Visual):
    label: Content
    value: Content
    longest_label: int
    indent: int = 2
    gutter: int = 1

    def _columns(self, width: int) -> tuple[int, int, int, list[Content], list[Content]]:
        usable_width = max(1, width - self.gutter)
        if usable_width < 5:
            return 0, usable_width, usable_width, self.label.wrap(usable_width), self.value.wrap(usable_width)
        indent = min(self.indent, max(0, usable_width - 5))
        usable = usable_width - indent - 2
        label_col = max(1, min(self.longest_label, usable // 2))
        value_col = max(1, usable - label_col)
        return indent, label_col, value_col, self.label.wrap(label_col), self.value.wrap(value_col)

    def content(self, width: int) -> Content:
        # OptionList has already subtracted option padding from the render width.
        usable_width = max(1, width - self.gutter)
        if usable_width < 5:
            return Content("\n").join([*self.label.wrap(usable_width), *self.value.wrap(usable_width)])
        indent, label_col, value_col, left, right = self._columns(width)
        lines: list[Content] = []
        rtl = is_rtl_locale()
        for index in range(max(len(left), len(right))):
            name = left[index] if index < len(left) else Content("")
            value = right[index] if index < len(right) else Content("")
            if rtl:
                v_pad = max(0, value_col - value.cell_length)
                l_pad = max(0, label_col - name.cell_length)
                lines.append(
                    Content(" " * v_pad) + value + Content("  ") + Content(" " * l_pad) + name + Content(" " * indent)
                )
            else:
                lines.append(
                    Content(" " * indent) + name + Content(" " * (label_col - name.cell_length + 2)) + value
                )
        return Content("\n").join(lines)

    def render_strips(self, width, height, style, options):
        usable_width = max(1, width - self.gutter)
        if usable_width < 5:
            strips = self.content(width).render_strips(width, height, style, options)
            rtl = is_rtl_locale()
            return [shape_strip(strip, rtl_base=rtl, logical=True) for strip in strips]
        indent, label_col, value_col, label_lines, value_lines = self._columns(width)
        rtl = is_rtl_locale()
        rich_style = style.rich_style
        row_count = max(len(label_lines), len(value_lines))
        if height is not None:
            row_count = min(row_count, height)
        strips: list[Strip] = []
        for index in range(row_count):
            name = label_lines[index] if index < len(label_lines) else Content("")
            value = value_lines[index] if index < len(value_lines) else Content("")
            name_rendered = name.render_strips(label_col, 1, style, options) if name else []
            value_rendered = value.render_strips(value_col, 1, style, options) if value else []
            name_strip = shape_strip(
                name_rendered[0] if name_rendered else Strip.blank(0, rich_style),
                rtl_base=rtl,
                logical=True,
                align_right=rtl,
            )
            value_strip = shape_strip(
                value_rendered[0] if value_rendered else Strip.blank(0, rich_style),
                rtl_base=rtl,
                logical=True,
                align_right=rtl,
            )
            if rtl:
                v_pad = max(0, value_col - value_strip.cell_length)
                l_pad = max(0, label_col - name_strip.cell_length)
                row = Strip.join(
                    [
                        Strip.blank(v_pad, rich_style),
                        value_strip,
                        Strip.blank(2, rich_style),
                        Strip.blank(l_pad, rich_style),
                        name_strip,
                        Strip.blank(indent, rich_style),
                    ]
                )
            else:
                gap = max(2, label_col - name_strip.cell_length + 2)
                row = Strip.join(
                    [
                        Strip.blank(indent, rich_style),
                        name_strip,
                        Strip.blank(gap, rich_style),
                        value_strip,
                    ]
                )
            strips.append(row)
        return strips

    def get_height(self, rules, width):
        return self.content(width).get_height(rules, width)

    def get_optimal_width(self, rules, container_width):
        return container_width
