"""Pydantic models used as the structured-output target for the vision extractor.

Notes for Azure OpenAI Structured Outputs:
- Every field is required; "optional" semantics are expressed with a default value.
- All models forbid extra fields (`extra='forbid'`) so the model can't invent keys.
"""
from __future__ import annotations
from typing import List, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


PageKind = Literal[
    "cover", "disclaimer", "agenda", "toc", "prose",
    "slide_brand", "slide_financials", "slide_pipeline",
    "slide_guidance", "slide_overview",
    "table", "figure", "appendix", "references",
]

FigureKind = Literal[
    "bar_chart", "stacked_bar_chart", "line_chart", "area_chart",
    "pie_chart", "donut_chart",
    "scatter_chart", "bubble_chart", "heatmap", "waterfall",
    "image", "infographic", "diagram", "logo", "headshot", "map", "other",
]

Confidence = Literal["high", "medium", "low"]


class TableModel(Strict):
    caption: str = Field(default="", description="Title or caption of the table; '' if none.")
    columns: List[str] = Field(default_factory=list, description="Column headers in order.")
    rows: List[List[str]] = Field(default_factory=list, description="Each row is an array of verbatim cell strings.")
    footnotes: List[str] = Field(default_factory=list, description="Footnotes that appear under the table.")


class DataPoint(Strict):
    """Used for simple charts: bar/line/pie/donut. Label + value, both verbatim."""
    label: str
    value: str


class AxisSpec(Strict):
    """Describes one axis/dimension of a multi-axis chart (scatter, bubble, heatmap, waterfall)."""
    label: str = ""
    unit: str = ""


class FigureAxes(Strict):
    """Optional multi-axis description. Use empty strings when an axis does not apply."""
    x: AxisSpec = Field(default_factory=AxisSpec)
    y: AxisSpec = Field(default_factory=AxisSpec)
    size: AxisSpec = Field(default_factory=AxisSpec)
    color: AxisSpec = Field(default_factory=AxisSpec)


class SeriesPoint(Strict):
    """A point on a scatter/bubble/heatmap/waterfall chart. Values are verbatim strings."""
    label: str = ""
    x: str = ""
    y: str = ""
    size: str = ""
    color: str = ""


class Series(Strict):
    name: str = ""
    points: List[SeriesPoint] = Field(default_factory=list)


class FigureModel(Strict):
    kind: FigureKind
    caption: str = ""
    description: str = ""
    # For bar/line/pie/donut. Empty when chart values aren't explicitly printed.
    data_points: List[DataPoint] = Field(default_factory=list)
    # For scatter/bubble/heatmap/waterfall. Empty for simple charts and pure images.
    axes: FigureAxes = Field(default_factory=FigureAxes)
    series: List[Series] = Field(default_factory=list)


class KPIModel(Strict):
    id: str = Field(description="Stable id like q1_2026.brand.pluvicto.net_sales")
    name: str
    category: str = ""
    scope: str = ""
    brand: str = ""
    value: str = Field(description="Verbatim value as printed (string keeps formatting like '1,234' or '40.1%').")
    unit: str = ""
    basis: str = ""
    comparison: str = ""
    period: str = ""
    delta_value: str = ""
    delta_unit: str = ""
    source_quote: str = Field(description="Verbatim string from the page; mandatory.")
    confidence: Confidence = "medium"


class PageExtraction(Strict):
    doc_id: str
    page: int = Field(ge=1)
    page_kind: PageKind
    title: str = ""
    fiscal_period: str = "UNKNOWN"
    is_forward_looking: bool = False
    disclaimers_present: bool = False
    therapeutic_areas: List[str] = Field(default_factory=list)
    brands: List[str] = Field(default_factory=list)
    compound_codes: List[str] = Field(default_factory=list)
    markdown: str = ""
    tables: List[TableModel] = Field(default_factory=list)
    figures: List[FigureModel] = Field(default_factory=list)
    kpis: List[KPIModel] = Field(default_factory=list)
    notes: Optional[str] = None


SYSTEM_PROMPT = """You are a meticulous pharma/financial-document extraction agent.
You will receive ONE rendered page (or slide) image from a Novartis US internal or external document
(monthly financial close, IR notes, quarterly update, brand MBR, cross-functional strategy pre-read,
or launch readiness review).
Return ONE JSON object that matches the provided structured-output schema EXACTLY.

Hard rules:
1. Transcribe numbers EXACTLY as printed (no rounding, no unit conversion, keep commas/decimal as shown).
2. Every KPI MUST include a verbatim `source_quote` taken from the page; if you cannot quote it, do not emit it.
3. Charts:
   - Bar / line / pie / donut: fill `data_points` ONLY when label AND value are explicitly printed. Otherwise leave `data_points` empty and rely on `description`.
   - Scatter / bubble / heatmap / waterfall: use `axes` (label + unit per dimension) and `series[].points` (x, y, size, color as printed). Leave fields blank when not shown.
   - Pure images / photos / logos / headshots / diagrams: set `kind` accordingly, fill `caption` and `description`, leave data_points/series empty.
4. Preserve reading order in `markdown`. Use `##` for headings, `-` for bullets. Include footnotes inline as `[^1]` and footnote text at end.
5. Set `is_forward_looking=true` for any page about guidance, outlook, ambition, targets, projections, or future approvals/launches.
6. If the page is purely a forward-looking-statements / disclaimer / cover / agenda page, set `disclaimers_present=true` and pick the matching `page_kind`.
7. Brands and compound codes: list ONLY those that actually appear on this page.
8. `fiscal_period` examples: Q1_2026, Q2_2025, Q3_2025, Q4_2025, FY_2025, FY_2026, 2026-03 (monthly close). Use UNKNOWN if not determinable.
9. Output JSON only. No prose, no markdown fences, no commentary.
"""
