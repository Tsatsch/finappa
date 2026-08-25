# Stats Section Tabs — Design

Date: 2026-08-25
Status: Approved
Amends: [2026-08-11-stats-website-design.md](2026-08-11-stats-website-design.md),
[2026-08-25-stats-month-compare-design.md](2026-08-25-stats-month-compare-design.md)

## Overview

Split the stuffed one-pager into three **in-page tabs** (hash-routed), and fix
the compare table’s clipped “CATEGORY” header.

## Tabs

Nav under the hero: **Overview** · **Compare** · **Merchants**.

| Tab | Hash | Contents |
|-----|------|----------|
| Overview | `#overview` (default) | Period filters, KPIs, This period charts, Trends |
| Compare | `#compare` | Compare months section only |
| Merchants | `#merchants` | Merchant overview only |

- One `/api/data` load; panels show/hide in CSS/JS.
- Changing tab sets `location.hash`; initial load reads hash.
- No new Starlette routes.

## CATEGORY header fix

Compare table must not inherit merchant-table `thead th { padding: 0 }` in a way
that clips the first header against the scroll container border. Give compare
headers explicit horizontal padding and ensure the scroll wrapper does not crop
label text.

## Non-goals

- Real `/compare` / `/merchants` routes.
- Budgets or other new analytics.
