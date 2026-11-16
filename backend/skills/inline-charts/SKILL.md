---
name: inline-charts
description: 主动为解释中的趋势、周期、占比和数值对比配交互图。Illustrate quantitative explanations in chat.
---

## Interactive charts in chat

When explaining a trend, periodic motion, parts of a whole, two related quantities, or a comparison that is easier to understand visually, include an interactive chart in the reply on your own initiative, even if the user only asks “why?” or “how does it work?”. For ordinary conversation, simple facts, or requests for text only, use prose.

Introduce the relationship, insert a top-level fenced `knowe-chart` JSON block exactly where the reader needs it, then explain one concrete point on the chart. Use the user's language for all visible labels. The chat client renders the block directly. No file or tool call is required to display it. Use available calculation or data tools when needed. Use actual values or samples calculated from a stated formula, never invented measurements. Identify illustrative assumptions.

For a known continuous function, **give the formula** and its x range. The client samples it exactly. For example, a spring with displacement 3 sin(2t) and its velocity would use:

```knowe-chart
{"type":"line","title":"弹簧的位移和速度","xLabel":"时间 (s)","x":{"start":0,"end":3.14159,"count":81},"series":[{"name":"位移","unit":"cm","formula":"3*sin(2*x)"},{"name":"速度","unit":"cm/s","axis":"right","formula":"6*cos(2*x)"}]}
```

Substitute the actual equations, units and interval; show a full period for periodic motion when applicable. Supported formula syntax: x, pi, numeric literals, parentheses, + - * /, and sin, cos, tan, sqrt, exp, log, abs in radians. Use explicit multiplication and the pi constant for π. Use at most two different units per chart (one per axis). If a third quantity explains a secondary point, discuss it in the prose or give it a separate chart.

For measured data, use an x array and a values array of equal length in each series instead of a formula. Numeric line coordinates must increase; use string labels for categories. Use type "bar" for discrete comparisons. Line, bar and mixed charts accept 2–500 x values and 1–6 series. Use just the quantity name in name and put its unit in unit. Write labels in the conversation language.

For **parts of one known whole**, choose "pie" to explain the shares, or "doughnut" when the total also matters (it appears in the center). Put category labels in x and the original amounts in one series. Hover shows the amount and percentage. Use nonnegative data with a positive total. Overlapping categories or independent percentages need a bar chart. Prefer a small set of meaningful categories.

```knowe-chart
{"type":"doughnut","title":"订单来源","x":["门店","网站","电话"],"series":[{"name":"订单","unit":"单","values":[60,30,10]}]}
```

For **volume and a related trend** on the same categories or dates, choose "mixed". Give every series its own type ("bar" or "line") and include both kinds. Put a rate with different units on the right axis. Keep x coordinates aligned.

```knowe-chart
{"type":"mixed","title":"订单量与转化率","x":["一月","二月","三月"],"series":[{"type":"bar","name":"订单量","unit":"单","values":[120,150,180]},{"type":"line","name":"转化率","unit":"%","axis":"right","values":[3,3.8,4.1]}]}
```

选图时，问“合计多少、各项构成怎样”就用 doughnut，一项金额只填一次原值，让界面算总额和占比。只问各类别占整体的份额用 pie。柱线混合图用于两个有独立含义的指标，例如订单量与转化率。
