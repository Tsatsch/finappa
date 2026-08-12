/* global Chart is provided by the Chart.js CDN script */

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

const PALETTE = [
  "#0f766e", "#d9480f", "#1d4e89", "#ca8a04", "#7c3aed",
  "#be123c", "#0e7490", "#15803d", "#c2410c", "#334155",
  "#0891b2", "#a16207", "#9333ea", "#e11d48",
];

let ledger = null;
let charts = {};
let overviewSort = { key: "cents", dir: "desc" };

function $(id) {
  return document.getElementById(id);
}

function formatCents(cents) {
  const sign = cents < 0 ? "−" : "";
  const abs = Math.abs(cents) / 100;
  const formatted = abs.toLocaleString("de-DE", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${sign}${formatted} €`;
}

function uniqueSorted(values) {
  return [...new Set(values)].sort((a, b) => (a > b ? 1 : a < b ? -1 : 0));
}

function fillSelect(select, options, { allLabel, allValue = "ALL" }) {
  select.innerHTML = "";
  const all = document.createElement("option");
  all.value = allValue;
  all.textContent = allLabel;
  select.appendChild(all);
  for (const opt of options) {
    const el = document.createElement("option");
    el.value = String(opt.value);
    el.textContent = opt.label;
    select.appendChild(el);
  }
}

function populateFilterSelects(transactions) {
  const years = uniqueSorted(transactions.map((t) => t.local_year));
  const categories = uniqueSorted(transactions.map((t) => t.category));
  const yearOptions = years.map((y) => ({ value: y, label: String(y) }));
  const monthOptions = MONTH_NAMES.map((name, idx) => ({
    value: idx + 1,
    label: name,
  }));
  const categoryOptions = categories.map((c) => ({ value: c, label: c }));

  for (const id of ["filter-year", "overview-filter-year"]) {
    fillSelect($(id), yearOptions, { allLabel: "All years" });
  }
  for (const id of ["filter-month", "overview-filter-month"]) {
    fillSelect($(id), monthOptions, { allLabel: "All months" });
  }
  for (const id of ["filter-category", "overview-filter-category"]) {
    fillSelect($(id), categoryOptions, { allLabel: "All categories" });
  }

  const now = new Date();
  const currentYear = String(now.getFullYear());
  const currentMonth = String(now.getMonth() + 1);
  if (years.map(String).includes(currentYear)) {
    $("filter-year").value = currentYear;
  }
  $("filter-month").value = currentMonth;

  $("overview-filter-year").value = "ALL";
  $("overview-filter-month").value = "ALL";
  $("overview-filter-category").value = "ALL";
}

function readFilters(prefix) {
  const year = $(`${prefix}-year`).value;
  const month = $(`${prefix}-month`).value;
  const category = $(`${prefix}-category`).value;
  const typeEl = $(`${prefix}-type`);
  const type = typeEl ? typeEl.value : "EXPENSE";
  return {
    year: year === "ALL" ? null : Number(year),
    month: month === "ALL" ? null : Number(month),
    category: category === "ALL" ? null : category,
    type: type === "ALL" ? null : type,
  };
}

function applyFilters(transactions, filters) {
  return transactions.filter((t) => {
    if (filters.year !== null && t.local_year !== filters.year) return false;
    if (filters.month !== null && t.local_month !== filters.month) return false;
    if (filters.category !== null && t.category !== filters.category) return false;
    if (filters.type !== null && t.type !== filters.type) return false;
    return true;
  });
}

function merchantLabel(t) {
  return t.merchant || t.notes || "—";
}

function dominantCategory(categories) {
  const entries = Object.entries(categories);
  if (entries.length === 0) return "—";
  entries.sort((a, b) => b[1] - a[1]);
  return entries[0][0];
}

function buildMerchantRows(transactions) {
  const byMerchant = {};
  for (const t of transactions) {
    if (t.type !== "EXPENSE") continue;
    const name = merchantLabel(t);
    if (!byMerchant[name]) {
      byMerchant[name] = { cents: 0, count: 0, categories: {} };
    }
    byMerchant[name].cents += t.amount_cents;
    byMerchant[name].count += 1;
    byMerchant[name].categories[t.category] =
      (byMerchant[name].categories[t.category] || 0) + t.amount_cents;
  }

  return Object.entries(byMerchant).map(([name, stats]) => ({
    name,
    cents: stats.cents,
    count: stats.count,
    avg: Math.round(stats.cents / stats.count),
    category: dominantCategory(stats.categories),
  }));
}

function sortMerchantRows(rows, sort) {
  const mult = sort.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    if (sort.key === "name") return mult * a.name.localeCompare(b.name, "de");
    if (sort.key === "category") return mult * a.category.localeCompare(b.category, "de");
    if (sort.key === "count") return mult * (a.count - b.count);
    if (sort.key === "avg") return mult * (a.avg - b.avg);
    return mult * (a.cents - b.cents);
  });
}

function summarize(transactions) {
  let income = 0;
  let expenses = 0;
  const byCategory = {};

  for (const t of transactions) {
    if (t.type === "INCOME") {
      income += t.amount_cents;
    } else if (t.type === "EXPENSE") {
      expenses += t.amount_cents;
      byCategory[t.category] = (byCategory[t.category] || 0) + t.amount_cents;
    }
  }

  return {
    income_cents: income,
    expense_cents: expenses,
    net_cents: income - expenses,
    by_category: byCategory,
  };
}

function destroyChart(key) {
  if (charts[key]) {
    charts[key].destroy();
    delete charts[key];
  }
}

function upsertChart(key, ctx, config) {
  destroyChart(key);
  charts[key] = new Chart(ctx, config);
}

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderMerchantChart(rows) {
  const card = $("merchant-card");
  if (rows.length === 0) {
    card.style.display = "none";
    destroyChart("merchant");
    return;
  }
  card.style.display = "";

  const top = sortMerchantRows(rows, { key: "cents", dir: "desc" }).slice(0, 12);
  upsertChart("merchant", $("chart-merchant"), {
    type: "bar",
    data: {
      labels: top.map((r) => r.name),
      datasets: [{
        label: "Expenses €",
        data: top.map((r) => r.cents / 100),
        backgroundColor: "#0f766e",
        borderRadius: 6,
      }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { beginAtZero: true },
        y: { grid: { display: false } },
      },
    },
  });
}

function updateSortButtons() {
  for (const btn of document.querySelectorAll(".sort-btn")) {
    const active = btn.dataset.sort === overviewSort.key;
    btn.classList.toggle("is-active", active);
    btn.dataset.dir = active ? overviewSort.dir : "";
  }
}

function renderOverviewTable() {
  const filters = readFilters("overview-filter");
  filters.type = "EXPENSE";
  let filtered = applyFilters(ledger.transactions, filters);
  const query = $("overview-search").value.trim().toLowerCase();
  if (query) {
    filtered = filtered.filter((t) => merchantLabel(t).toLowerCase().includes(query));
  }

  const rows = sortMerchantRows(buildMerchantRows(filtered), overviewSort);
  const body = $("overview-table-body");
  body.innerHTML = "";

  if (rows.length === 0) {
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="5" class="empty">No merchants match.</td>';
    body.appendChild(row);
  } else {
    for (const row of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${escapeHtml(row.name)}</td>` +
        `<td>${escapeHtml(row.category)}</td>` +
        `<td class="num">${formatCents(row.cents)}</td>` +
        `<td class="num">${row.count}</td>` +
        `<td class="num">${formatCents(row.avg)}</td>`;
      body.appendChild(tr);
    }
  }

  const totalCents = rows.reduce((sum, r) => sum + r.cents, 0);
  $("overview-meta").textContent =
    `${rows.length} merchants · ${formatCents(totalCents)} total expenses`;
  updateSortButtons();
}

function renderCategoryChart(byCategory) {
  const entries = Object.entries(byCategory)
    .filter(([, cents]) => cents !== 0)
    .sort((a, b) => b[1] - a[1]);
  upsertChart("category", $("chart-category"), {
    type: "doughnut",
    data: {
      labels: entries.map(([name]) => name),
      datasets: [{
        data: entries.map(([, cents]) => cents / 100),
        backgroundColor: entries.map((_, i) => PALETTE[i % PALETTE.length]),
        borderWidth: 0,
      }],
    },
    options: {
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12 } },
      },
    },
  });
}

function renderDailyChart(transactions, filters) {
  const card = $("daily-card");
  if (filters.month === null) {
    card.style.display = "none";
    destroyChart("daily");
    return;
  }
  card.style.display = "";

  const daysInMonth = new Date(
    filters.year || new Date().getFullYear(),
    filters.month,
    0,
  ).getDate();
  const byDay = Array.from({ length: daysInMonth }, () => 0);

  for (const t of transactions) {
    const day = Number(t.local_date.slice(8, 10));
    if (t.type === "EXPENSE") byDay[day - 1] += t.amount_cents;
  }

  upsertChart("daily", $("chart-daily"), {
    type: "bar",
    data: {
      labels: byDay.map((_, i) => String(i + 1)),
      datasets: [{
        label: "Expenses €",
        data: byDay.map((c) => c / 100),
        backgroundColor: "#0f766e",
        borderRadius: 6,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true },
      },
    },
  });
}

function monthKey(t) {
  return `${t.local_year}-${String(t.local_month).padStart(2, "0")}`;
}

function renderMonthlyChart(transactions) {
  const keys = uniqueSorted(transactions.map(monthKey));
  const income = Object.fromEntries(keys.map((k) => [k, 0]));
  const expenses = Object.fromEntries(keys.map((k) => [k, 0]));

  for (const t of transactions) {
    const key = monthKey(t);
    if (t.type === "INCOME") income[key] += t.amount_cents;
    if (t.type === "EXPENSE") expenses[key] += t.amount_cents;
  }

  upsertChart("monthly", $("chart-monthly"), {
    type: "bar",
    data: {
      labels: keys,
      datasets: [
        {
          label: "Income",
          data: keys.map((k) => income[k] / 100),
          backgroundColor: "#0f766e",
          borderRadius: 6,
        },
        {
          label: "Expenses",
          data: keys.map((k) => expenses[k] / 100),
          backgroundColor: "#d9480f",
          borderRadius: 6,
        },
      ],
    },
    options: {
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true },
      },
    },
  });
}

function renderCategoryTrend(transactions) {
  const keys = uniqueSorted(transactions.map(monthKey));
  const categories = uniqueSorted(
    transactions.filter((t) => t.type === "EXPENSE").map((t) => t.category),
  );
  const series = Object.fromEntries(
    categories.map((c) => [c, Object.fromEntries(keys.map((k) => [k, 0]))]),
  );

  for (const t of transactions) {
    if (!series[t.category]) continue;
    const key = monthKey(t);
    if (t.type === "EXPENSE") series[t.category][key] += t.amount_cents;
  }

  upsertChart("categoryTrend", $("chart-category-trend"), {
    type: "bar",
    data: {
      labels: keys,
      datasets: categories.map((cat, i) => ({
        label: cat,
        data: keys.map((k) => series[cat][k] / 100),
        backgroundColor: PALETTE[i % PALETTE.length],
        stack: "categories",
        borderWidth: 0,
      })),
    },
    options: {
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: { stacked: true, beginAtZero: true },
      },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12 } },
      },
    },
  });
}

function renderPeriod() {
  const filters = readFilters("filter");
  const filtered = applyFilters(ledger.transactions, filters);
  const summary = summarize(filtered);

  $("kpi-income").textContent = formatCents(summary.income_cents);
  $("kpi-expenses").textContent = formatCents(summary.expense_cents);
  $("kpi-net").textContent = formatCents(summary.net_cents);

  const when = new Date(ledger.generated_at);
  $("meta").textContent =
    `${filtered.length} transactions in view · refreshed ${when.toLocaleString()} · ${ledger.timezone}`;

  renderCategoryChart(summary.by_category);
  renderMerchantChart(buildMerchantRows(filtered));
  renderDailyChart(filtered, filters);
  renderMonthlyChart(filtered);
  renderCategoryTrend(filtered);
}

function render() {
  if (!ledger) return;
  renderPeriod();
  renderOverviewTable();
}

async function boot() {
  const response = await fetch("/api/data");
  if (!response.ok) {
    $("meta").textContent = `Failed to load data (${response.status})`;
    return;
  }
  ledger = await response.json();
  populateFilterSelects(ledger.transactions);

  for (const id of ["filter-year", "filter-month", "filter-category", "filter-type"]) {
    $(id).addEventListener("change", renderPeriod);
  }
  for (const id of ["overview-filter-year", "overview-filter-month", "overview-filter-category"]) {
    $(id).addEventListener("change", renderOverviewTable);
  }
  $("overview-search").addEventListener("input", renderOverviewTable);

  for (const btn of document.querySelectorAll(".sort-btn")) {
    btn.addEventListener("click", () => {
      const key = btn.dataset.sort;
      if (overviewSort.key === key) {
        overviewSort.dir = overviewSort.dir === "asc" ? "desc" : "asc";
      } else {
        overviewSort = { key, dir: key === "name" || key === "category" ? "asc" : "desc" };
      }
      renderOverviewTable();
    });
  }

  render();
}

boot();
