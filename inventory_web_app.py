#!/usr/bin/env python3
import csv
import io
import json
import sqlite3
import os
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = APP_DIR / "inventory.sqlite3"


def setup_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            style TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL DEFAULT '個',
            cost REAL NOT NULL DEFAULT 0,
            price REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            sale_id INTEGER REFERENCES sales(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('purchase', 'sale', 'adjust')),
            qty REAL NOT NULL,
            unit_cost REAL NOT NULL DEFAULT 0,
            unit_price REAL NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            happened_on TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_name TEXT NOT NULL DEFAULT '',
            collector TEXT NOT NULL DEFAULT '',
            line_pay INTEGER NOT NULL DEFAULT 0,
            donation REAL NOT NULL DEFAULT 0,
            sold_on TEXT NOT NULL,
            total_items REAL NOT NULL DEFAULT 0,
            total_amount REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            product_name TEXT NOT NULL,
            product_style TEXT NOT NULL DEFAULT '',
            qty REAL NOT NULL,
            unit_price REAL NOT NULL,
            unit_cost REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(products)").fetchall()}
    if "style" not in columns:
        db.execute("ALTER TABLE products ADD COLUMN style TEXT NOT NULL DEFAULT ''")
    movement_columns = {row[1] for row in db.execute("PRAGMA table_info(movements)").fetchall()}
    if "sale_id" not in movement_columns:
        db.execute("ALTER TABLE movements ADD COLUMN sale_id INTEGER REFERENCES sales(id) ON DELETE CASCADE")
    sale_columns = {row[1] for row in db.execute("PRAGMA table_info(sales)").fetchall()}
    if "event_name" not in sale_columns:
        db.execute("ALTER TABLE sales ADD COLUMN event_name TEXT NOT NULL DEFAULT ''")
    db.commit()
    db.close()


def conn():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def dicts(rows):
    return [dict(row) for row in rows]


def previous_day(day_text):
    day = datetime.strptime(day_text, "%Y-%m-%d").date()
    return day.fromordinal(day.toordinal() - 1).isoformat()


def products(active_only=True):
    where = "WHERE active = 1" if active_only else ""
    with conn() as db:
        return dicts(
            db.execute(
                f"""
                SELECT p.*,
                       COALESCE(SUM(CASE
                           WHEN m.kind = 'purchase' THEN m.qty
                           WHEN m.kind = 'sale' THEN -m.qty
                           WHEN m.kind = 'adjust' THEN m.qty
                           ELSE 0
                       END), 0) AS stock
                FROM products p
                LEFT JOIN movements m ON m.product_id = p.id
                {where}
                GROUP BY p.id
                ORDER BY p.name COLLATE NOCASE, p.style COLLATE NOCASE, p.sku COLLATE NOCASE
                """
            ).fetchall()
        )


def movements():
    with conn() as db:
        return dicts(
            db.execute(
                """
                SELECT m.*, p.sku, p.name, p.style, p.unit
                FROM movements m
                JOIN products p ON p.id = m.product_id
                ORDER BY m.happened_on DESC, m.id DESC
                LIMIT 300
                """
            ).fetchall()
        )


def sales():
    with conn() as db:
        sale_rows = dicts(
            db.execute(
                """
                SELECT *
                FROM sales
                ORDER BY sold_on DESC, id DESC
                LIMIT 200
                """
            ).fetchall()
        )
        for sale in sale_rows:
            sale["items"] = dicts(
                db.execute(
                    """
                    SELECT product_name, product_style, qty, unit_price
                    FROM sale_items
                    WHERE sale_id = ?
                    ORDER BY id
                    """,
                    (sale["id"],),
                ).fetchall()
            )
        return sale_rows


def collectors():
    with conn() as db:
        cleared_at = db.execute(
            "SELECT value FROM app_settings WHERE key = 'collectors_cleared_at'"
        ).fetchone()
        params = []
        filter_sql = ""
        if cleared_at:
            filter_sql = "AND created_at > ?"
            params.append(cleared_at["value"])
        return [
            row["collector"]
            for row in db.execute(
                f"""
                SELECT DISTINCT collector
                FROM sales
                WHERE TRIM(collector) != ''
                {filter_sql}
                ORDER BY collector COLLATE NOCASE
                """,
                params,
            ).fetchall()
        ]


def stock_until(product_id, until_day):
    with conn() as db:
        return db.execute(
            """
            SELECT COALESCE(SUM(CASE
                WHEN kind = 'purchase' THEN qty
                WHEN kind = 'sale' THEN -qty
                WHEN kind = 'adjust' THEN qty
                ELSE 0
            END), 0)
            FROM movements
            WHERE product_id = ? AND happened_on <= ?
            """,
            (product_id, until_day),
        ).fetchone()[0]


def daily_summary(day):
    totals = {
        "sales_amount": 0,
        "donation_amount": 0,
        "total_received": 0,
        "line_pay_amount": 0,
        "cash_amount": 0,
        "events": "",
    }
    with conn() as db:
        sale_totals = db.execute(
            """
            SELECT
                COALESCE(SUM(donation), 0) AS donation_amount,
                COALESCE(SUM(total_amount), 0) AS total_received,
                COALESCE(SUM(CASE WHEN line_pay = 1 THEN total_amount ELSE 0 END), 0) AS line_pay_amount,
                COALESCE(SUM(CASE WHEN line_pay = 0 THEN total_amount ELSE 0 END), 0) AS cash_amount
            FROM sales
            WHERE sold_on = ?
            """,
            (day,),
        ).fetchone()
        totals["donation_amount"] = sale_totals["donation_amount"]
        totals["total_received"] = sale_totals["total_received"]
        totals["line_pay_amount"] = sale_totals["line_pay_amount"]
        totals["cash_amount"] = sale_totals["cash_amount"]
        event_rows = db.execute(
            "SELECT DISTINCT event_name FROM sales WHERE sold_on = ? AND TRIM(event_name) != '' ORDER BY event_name",
            (day,),
        ).fetchall()
        totals["events"] = "、".join(row["event_name"] for row in event_rows)
        rows = dicts(
            db.execute(
                """
                SELECT
                    si.product_name AS name,
                    si.product_style AS style,
                    COALESCE(SUM(si.qty), 0) AS sale_qty,
                    COALESCE(SUM(si.qty * si.unit_price), 0) AS sales_amount
                FROM sale_items si
                JOIN sales s ON s.id = si.sale_id
                WHERE s.sold_on = ?
                GROUP BY si.product_name, si.product_style
                ORDER BY si.product_name COLLATE NOCASE, si.product_style COLLATE NOCASE
                """,
                (day,),
            ).fetchall()
        )
    return {"rows": rows, "totals": totals}


HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>擺攤收款與進銷存</title>
  <style>
    :root {
      color-scheme: light;
      --bg:#FFFEEC;
      --panel:#fffef5;
      --line:#e8df9a;
      --text:#231815;
      --muted:#6f675f;
      --brand:#FFF100;
      --brand-strong:#FFD80E;
      --green:#71A840;
      --red:#b42318;
    }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Noto Sans TC",Arial,sans-serif; font-size:15px; }
    header { padding:18px 24px 12px; border-bottom:3px solid var(--text); background:var(--brand); display:flex; align-items:flex-end; justify-content:space-between; gap:16px; }
    h1 { margin:0; font-size:24px; line-height:1.2; }
    main { padding:18px 24px 28px; }
    label { display:block; color:#344054; font-weight:700; margin-bottom:5px; font-size:13px; }
    input, select { width:100%; height:38px; border:1px solid #cfc46d; border-radius:6px; padding:0 9px; font-size:15px; background:#fff; color:var(--text); }
    button { height:38px; border:1px solid #cfc46d; background:#fff; border-radius:6px; padding:0 13px; font-size:15px; cursor:pointer; color:var(--text); }
    button.primary { border-color:var(--text); background:var(--brand); color:var(--text); font-weight:800; }
    button.green { border-color:var(--green); background:var(--green); color:white; font-weight:700; }
    button.warn { border-color:#f3b0aa; color:var(--red); }
    .path, .muted { color:var(--muted); }
    .path { font-size:13px; word-break:break-all; }
    .tabs { display:flex; gap:8px; border-bottom:2px solid var(--line); margin-bottom:16px; overflow:auto; }
    .tab { border:0; background:transparent; padding:10px 14px; color:#6f675f; border-bottom:4px solid transparent; white-space:nowrap; }
    .tab.active { color:var(--text); border-bottom-color:var(--brand-strong); font-weight:900; }
    .view { display:none; }
    .view.active { display:block; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:14px; }
    .form { display:grid; grid-template-columns:repeat(6, minmax(120px, 1fr)); gap:12px; align-items:end; }
    .sale-form { display:grid; grid-template-columns:1.2fr 2fr 150px 150px 150px; gap:12px; align-items:end; }
    .line-form { display:grid; grid-template-columns:1.8fr 1.4fr 110px 130px 96px; gap:10px; align-items:end; }
    .collector-field { display:flex; gap:8px; align-items:end; }
    .collector-field input { flex:1; min-width:260px; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
    .table-wrap { overflow:auto; background:#fff; border:1px solid var(--line); border-radius:8px; margin-bottom:14px; }
    table { width:100%; border-collapse:collapse; min-width:760px; }
    th, td { padding:10px 11px; border-bottom:1px solid #edf0f5; text-align:left; white-space:nowrap; }
    th { background:var(--brand); color:var(--text); font-size:13px; position:sticky; top:0; }
    tr:hover td { background:#fffbd1; }
    .right { text-align:right; }
    .totals { display:grid; grid-template-columns:repeat(6, 1fr); gap:12px; margin-bottom:14px; }
    .metric { background:#fff; border:1px solid var(--line); border-top:5px solid var(--brand); border-radius:8px; padding:12px 14px; }
    .metric span { display:block; color:var(--muted); font-size:13px; margin-bottom:5px; }
    .metric strong { font-size:22px; }
    .big-total { font-size:28px; font-weight:900; color:var(--green); }
    @media (max-width: 1000px) { .form, .sale-form, .line-form { grid-template-columns:repeat(2, minmax(130px, 1fr)); } .totals { grid-template-columns:repeat(2, 1fr); } header { display:block; } }
  </style>
</head>
<body>
  <header>
    <div><h1>擺攤收款紀錄</h1><div class="muted">收款、捐款、LINE Pay 與每日結算</div></div>
    <div class="path">資料庫：data/inventory.sqlite3</div>
  </header>
  <main>
    <nav class="tabs">
      <button class="tab active" data-view="sale">擺攤收款</button>
      <button class="tab" data-view="products">商品 / 款式</button>
      <button class="tab" data-view="summary">每日結算</button>
    </nav>

    <section id="sale" class="view active">
      <div class="panel sale-form">
        <div><label>活動名稱</label><input id="eventName" placeholder="例如：華山領養日"></div>
        <div class="collector-cell"><label>收款人</label><div class="collector-field"><input id="collector" list="collectorOptions" placeholder="可輸入或下拉選擇"><button type="button" onclick="clearCollectorHistory()">清除紀錄</button></div><datalist id="collectorOptions"></datalist></div>
        <div><label>是否使用 LINE Pay?</label><select id="linePay"><option value="0">否，現金</option><option value="1">是，LINE Pay</option></select></div>
        <div><label>捐款金額</label><input id="donation" type="number" min="0" value="0" oninput="renderSaleLines()"></div>
        <div><label>日期</label><input id="saleDate" type="date"></div>
      </div>

      <div class="panel">
        <div class="line-form">
          <div><label>購買商品</label><select id="saleProduct"></select></div>
          <div><label>款式</label><select id="saleStyle"></select></div>
          <div><label>數量</label><input id="saleQty" type="number" min="1" value="1"></div>
          <div><label>單價</label><input id="salePrice" type="number" min="0" value="0"></div>
          <button class="green" onclick="addSaleLine()">加入</button>
        </div>
      </div>

      <div class="table-wrap">
        <table><thead><tr><th>商品</th><th>款式</th><th class="right">數量</th><th class="right">單價</th><th class="right">小計</th><th></th></tr></thead><tbody id="saleLineRows"></tbody></table>
      </div>
      <div class="panel actions" style="justify-content:space-between">
        <div>
          <div class="muted">總金額 = 商品總價 + 捐款金額</div>
          <div class="big-total" id="saleTotal">0</div>
        </div>
        <button class="primary" onclick="saveSale()">儲存這筆收款</button>
      </div>
      <div class="table-wrap">
        <table><thead><tr><th>日期</th><th>活動</th><th>收款人</th><th>付款方式</th><th>內容</th><th class="right">商品總價</th><th class="right">捐款</th><th class="right">總金額</th><th></th></tr></thead><tbody id="saleRows"></tbody></table>
      </div>
    </section>

    <section id="products" class="view">
      <div class="panel form">
        <input type="hidden" id="productId">
        <div><label>商品編號</label><input id="sku"></div>
        <div><label>商品名稱</label><input id="name"></div>
        <div><label>款式</label><input id="styleName" placeholder="例如：黑色 / S / A款"></div>
        <div><label>單位</label><input id="unit" value="個"></div>
        <div><label>售價</label><input id="price" type="number" value="0"></div>
        <div class="actions"><button class="primary" onclick="saveProduct()">新增 / 更新</button><button onclick="clearProduct()">清空</button></div>
      </div>
      <div class="actions" style="margin-bottom:10px"><button class="warn" onclick="deactivateProduct()">停用選取商品</button><button class="warn" onclick="deleteProduct()">刪除選取商品</button></div>
      <div class="table-wrap"><table><thead><tr><th>選取</th><th>編號</th><th>商品</th><th>款式</th><th>單位</th><th class="right">售價</th></tr></thead><tbody id="productRows"></tbody></table></div>
    </section>

    <section id="summary" class="view">
      <div class="panel actions"><label style="margin:0">結算日期</label><input id="sDate" type="date" style="width:170px"><button class="primary" onclick="loadSummary()">產生結算</button><button onclick="location.href='/export/summary?day=' + document.getElementById('sDate').value">匯出結算 CSV</button></div>
      <div class="panel"><strong>活動：</strong><span id="summaryEvents" class="muted"></span></div>
      <div class="totals">
        <div class="metric"><span>商品銷售</span><strong id="tSales">0</strong></div>
        <div class="metric"><span>捐款</span><strong id="tDonation">0</strong></div>
        <div class="metric"><span>總收入</span><strong id="tReceived">0</strong></div>
        <div class="metric"><span>LINE Pay</span><strong id="tLine">0</strong></div>
        <div class="metric"><span>現金</span><strong id="tCash">0</strong></div>
      </div>
      <div class="table-wrap"><table><thead><tr><th>商品</th><th>款式</th><th class="right">售出數量</th><th class="right">銷售金額</th></tr></thead><tbody id="summaryRows"></tbody></table></div>
    </section>
  </main>
  <script>
    let productData = [];
    let saleLines = [];
    const q = id => document.getElementById(id);
    const today = new Date().toISOString().slice(0, 10);
    q('saleDate').value = today; q('sDate').value = today;
    document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
      document.querySelectorAll('.tab,.view').forEach(el => el.classList.remove('active'));
      btn.classList.add('active'); document.querySelector('#' + btn.dataset.view).classList.add('active');
    }));
    const fmt = n => Number(n || 0).toLocaleString('zh-TW', { maximumFractionDigits: 2 });
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const label = p => `${p.name}${p.style ? ' - ' + p.style : ''}`;
    async function api(path, data) {
      const opt = data
        ? {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)}
        : {credentials:'same-origin'};
      const res = await fetch(path, opt);
      const text = await res.text();
      let out;
      try {
        out = JSON.parse(text);
      } catch (err) {
        throw new Error(`讀取資料失敗：伺服器回傳了網頁錯誤頁（HTTP ${res.status}）。請到 Render 的 Logs 看紅色錯誤。`);
      }
      if (!res.ok || out.error) throw new Error(out.error || '操作失敗');
      return out;
    }
    async function loadAll() {
      try {
        productData = await api('/api/products');
        renderProducts(); renderProductOptions();
        renderSales(await api('/api/sales'));
        renderCollectors(await api('/api/collectors'));
        await loadSummary();
      } catch (err) {
        alert(err.message);
      }
    }
    function renderProducts() {
      const selectedId = q('productId').value;
      q('productRows').innerHTML = productData.map(p => `<tr onclick="editProduct(${p.id})"><td><input type="radio" name="selectedProduct" ${String(p.id) === String(selectedId) ? 'checked' : ''} aria-label="選取商品"></td><td>${esc(p.sku)}</td><td>${esc(p.name)}</td><td>${esc(p.style)}</td><td>${esc(p.unit)}</td><td class="right">${fmt(p.price)}</td></tr>`).join('');
    }
    function renderProductOptions() {
      const productNames = [...new Set(productData.map(p => p.name))];
      const saleOptions = productNames.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
      q('saleProduct').innerHTML = saleOptions;
      renderSaleStyles();
    }
    function renderCollectors(rows) {
      q('collectorOptions').innerHTML = rows.map(name => `<option value="${esc(name)}"></option>`).join('');
    }
    async function clearCollectorHistory() {
      if (!confirm('確定清除收款人下拉紀錄？已儲存的收款資料不會被刪除。')) return;
      try {
        await api('/api/collectors/clear', {});
        q('collectorOptions').innerHTML = '';
        q('collector').value = '';
      } catch (err) {
        alert(err.message);
      }
    }
    function renderSales(rows) {
      q('saleRows').innerHTML = rows.map(s => {
        const items = s.items.map(i => `${esc(i.product_name)}${i.product_style ? ' / ' + esc(i.product_style) : ''} x ${fmt(i.qty)}`).join('、');
        return `<tr><td>${s.sold_on}</td><td>${esc(s.event_name || '')}</td><td>${esc(s.collector || '')}</td><td>${s.line_pay ? 'LINE Pay' : '現金'}</td><td>${items}</td><td class="right">${fmt(s.total_amount - s.donation)}</td><td class="right">${fmt(s.donation)}</td><td class="right">${fmt(s.total_amount)}</td><td class="right"><button type="button" class="warn sale-delete" data-sale-id="${s.id}">刪除</button></td></tr>`;
      }).join('');
      q('saleRows').querySelectorAll('.sale-delete').forEach(btn => {
        btn.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          deleteSale(btn.dataset.saleId);
        });
      });
    }
    function editProduct(id) {
      const p = productData.find(x => x.id === id);
      q('productId').value = p.id; q('sku').value = p.sku; q('name').value = p.name; q('styleName').value = p.style; q('unit').value = p.unit; q('price').value = p.price;
      renderProducts();
    }
    function clearProduct() {
      q('productId').value = ''; q('sku').value = ''; q('name').value = ''; q('styleName').value = ''; q('unit').value = '個'; q('price').value = 0;
      renderProducts();
    }
    async function saveProduct() {
      try {
        await api('/api/product', {id:q('productId').value, sku:q('sku').value, name:q('name').value, style:q('styleName').value, unit:q('unit').value, cost:0, price:q('price').value});
        clearProduct(); await loadAll();
        alert('商品已儲存。');
      } catch (err) {
        alert(err.message);
      }
    }
    async function deactivateProduct() {
      if (!q('productId').value) return alert('請先點選要停用的商品。');
      if (!confirm('停用後不會出現在選單，歷史資料仍會保留。確定停用？')) return;
      await api('/api/product/deactivate', {id:q('productId').value});
      clearProduct(); await loadAll();
    }
    async function deleteProduct() {
      if (!q('productId').value) return alert('請先點選要刪除的商品。');
      if (!confirm('確定刪除這個商品？如果已有收款紀錄，系統會從清單隱藏並保留歷史紀錄。')) return;
      try {
        await api('/api/product/delete', {id:q('productId').value});
        clearProduct(); await loadAll();
        alert('商品已從清單移除。');
      } catch (err) {
        alert(err.message);
      }
    }
    function productsForSelectedName() {
      return productData.filter(p => p.name === q('saleProduct').value);
    }
    function selectedSaleProduct() {
      return productData.find(p => String(p.id) === String(q('saleStyle').value)) || productsForSelectedName()[0];
    }
    function renderSaleStyles() {
      const rows = productsForSelectedName();
      q('saleStyle').innerHTML = rows.map(p => `<option value="${p.id}">${esc(p.style || '無款式')}</option>`).join('');
      fillSaleProduct();
    }
    function fillSaleProduct() {
      const p = selectedSaleProduct();
      if (p) q('salePrice').value = p.price;
    }
    q('saleProduct').addEventListener('change', renderSaleStyles);
    q('saleStyle').addEventListener('change', fillSaleProduct);
    function addSaleLine() {
      const p = selectedSaleProduct();
      if (!p) return alert('請先新增商品 / 款式。');
      const qty = Number(q('saleQty').value || 0);
      const unitPrice = Number(q('salePrice').value || 0);
      if (qty <= 0) return alert('數量要大於 0。');
      saleLines.push({product_id:p.id, product_name:p.name, product_style:p.style, qty, unit_price:unitPrice, unit_cost:Number(p.cost || 0)});
      q('saleQty').value = 1; renderSaleLines();
    }
    function removeSaleLine(index) {
      saleLines.splice(index, 1); renderSaleLines();
    }
    function renderSaleLines() {
      q('saleLineRows').innerHTML = saleLines.map((l, i) => `<tr><td>${esc(l.product_name)}</td><td>${esc(l.product_style)}</td><td class="right">${fmt(l.qty)}</td><td class="right">${fmt(l.unit_price)}</td><td class="right">${fmt(l.qty * l.unit_price)}</td><td class="right"><button class="warn" onclick="removeSaleLine(${i})">移除</button></td></tr>`).join('');
      const itemTotal = saleLines.reduce((sum, l) => sum + l.qty * l.unit_price, 0);
      q('saleTotal').textContent = fmt(itemTotal + Number(q('donation').value || 0));
    }
    async function saveSale() {
      if (!saleLines.length) return alert('請先加入購買商品。');
      await api('/api/sale', {event_name:q('eventName').value, collector:q('collector').value, line_pay:q('linePay').value, donation:q('donation').value, sold_on:q('saleDate').value, items:saleLines});
      saleLines = []; q('donation').value = 0; renderSaleLines(); await loadAll();
    }
    async function deleteSale(id) {
      if (!confirm('確定刪除這筆收款？')) return;
      try {
        await api('/api/sale/delete', {id});
        await loadAll();
      } catch (err) {
        alert(err.message);
      }
    }
    async function loadSummary() {
      const out = await api('/api/summary?day=' + encodeURIComponent(q('sDate').value));
      q('tSales').textContent = fmt(out.totals.sales_amount);
      q('tDonation').textContent = fmt(out.totals.donation_amount);
      q('tReceived').textContent = fmt(out.totals.total_received);
      q('tLine').textContent = fmt(out.totals.line_pay_amount);
      q('tCash').textContent = fmt(out.totals.cash_amount);
      q('summaryEvents').textContent = out.totals.events || '尚無活動名稱';
      q('summaryRows').innerHTML = out.rows.map(r => `<tr><td>${esc(r.name)}</td><td>${esc(r.style)}</td><td class="right">${fmt(r.sale_qty)}</td><td class="right">${fmt(r.sales_amount)}</td></tr>`).join('');
    }
    renderSaleLines();
    loadAll();
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                body = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/products":
                self.send_json(products())
            elif parsed.path == "/api/sales":
                self.send_json(sales())
            elif parsed.path == "/api/collectors":
                self.send_json(collectors())
            elif parsed.path == "/api/summary":
                day = parse_qs(parsed.query).get("day", [date.today().isoformat()])[0]
                self.send_json(daily_summary(day))
            elif parsed.path == "/export/summary":
                day = parse_qs(parsed.query).get("day", [date.today().isoformat()])[0]
                self.export_summary(day)
            else:
                self.send_json({"error": "找不到頁面"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        try:
            data = self.read_json()
            if self.path == "/api/product":
                self.save_product(data)
            elif self.path == "/api/product/deactivate":
                with conn() as db:
                    db.execute("UPDATE products SET active=0 WHERE id=?", (int(data["id"]),))
                    db.commit()
                self.send_json({"ok": True})
            elif self.path == "/api/product/delete":
                self.delete_product(data)
            elif self.path == "/api/sale":
                self.save_sale(data)
            elif self.path == "/api/sale/delete":
                self.delete_sale(data)
            elif self.path == "/api/collectors/clear":
                self.clear_collectors()
            else:
                self.send_json({"error": "找不到功能"}, 404)
        except sqlite3.IntegrityError:
            self.send_json({"error": "商品編號已存在，請換一個編號。"}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def save_product(self, data):
        sku = data.get("sku", "").strip()
        name = data.get("name", "").strip()
        if not sku or not name:
            raise ValueError("請輸入商品編號與名稱")
        with conn() as db:
            if data.get("id"):
                db.execute(
                    "UPDATE products SET sku=?, name=?, style=?, unit=?, cost=?, price=? WHERE id=?",
                    (sku, name, data.get("style", "").strip(), data.get("unit") or "個", float(data.get("cost") or 0), float(data.get("price") or 0), int(data["id"])),
                )
            else:
                db.execute(
                    "INSERT INTO products (sku, name, style, unit, cost, price, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sku, name, data.get("style", "").strip(), data.get("unit") or "個", float(data.get("cost") or 0), float(data.get("price") or 0), datetime.now().isoformat(timespec="seconds")),
                )
            db.commit()
        self.send_json({"ok": True})

    def delete_product(self, data):
        product_id = int(data["id"])
        with conn() as db:
            movement_count = db.execute("SELECT COUNT(*) FROM movements WHERE product_id=?", (product_id,)).fetchone()[0]
            sale_item_count = db.execute("SELECT COUNT(*) FROM sale_items WHERE product_id=?", (product_id,)).fetchone()[0]
            if movement_count or sale_item_count:
                db.execute("UPDATE products SET active=0 WHERE id=?", (product_id,))
            else:
                db.execute("DELETE FROM products WHERE id=?", (product_id,))
            db.commit()
        self.send_json({"ok": True})

    def save_sale(self, data):
        items = data.get("items") or []
        if not items:
            raise ValueError("請先加入購買商品")
        sold_on = data.get("sold_on") or date.today().isoformat()
        datetime.strptime(sold_on, "%Y-%m-%d")
        donation = float(data.get("donation") or 0)
        if donation < 0:
            raise ValueError("捐款金額不可小於 0")
        with conn() as db:
            product_ids = [int(item["product_id"]) for item in items]
            product_rows = {
                row["id"]: row
                for row in db.execute(
                    f"SELECT * FROM products WHERE id IN ({','.join('?' for _ in product_ids)})",
                    product_ids,
                ).fetchall()
            }
            item_total = 0
            total_qty = 0
            clean_items = []
            for item in items:
                product_id = int(item["product_id"])
                product = product_rows.get(product_id)
                if not product:
                    raise ValueError("找不到商品")
                qty = float(item.get("qty") or 0)
                unit_price = float(item.get("unit_price") or 0)
                if qty <= 0:
                    raise ValueError("商品數量要大於 0")
                item_total += qty * unit_price
                total_qty += qty
                clean_items.append((product, qty, unit_price))

            now = datetime.now().isoformat(timespec="seconds")
            cur = db.execute(
                """
                INSERT INTO sales (event_name, collector, line_pay, donation, sold_on, total_items, total_amount, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (data.get("event_name", "").strip(), data.get("collector", "").strip(), 1 if str(data.get("line_pay")) == "1" else 0, donation, sold_on, total_qty, item_total + donation, now),
            )
            sale_id = cur.lastrowid
            for product, qty, unit_price in clean_items:
                db.execute(
                    """
                    INSERT INTO sale_items (sale_id, product_id, product_name, product_style, qty, unit_price, unit_cost)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (sale_id, product["id"], product["name"], product["style"], qty, unit_price, product["cost"]),
                )
                db.execute(
                    """
                    INSERT INTO movements (product_id, sale_id, kind, qty, unit_cost, unit_price, note, happened_on, created_at)
                    VALUES (?, ?, 'sale', ?, ?, ?, ?, ?, ?)
                    """,
                    (product["id"], sale_id, qty, product["cost"], unit_price, f"收款#{sale_id} {data.get('collector', '').strip()}", sold_on, now),
                )
            db.commit()
        self.send_json({"ok": True})

    def delete_sale(self, data):
        sale_id = int(data["id"])
        with conn() as db:
            exists = db.execute("SELECT id FROM sales WHERE id=?", (sale_id,)).fetchone()
            if not exists:
                raise ValueError("找不到這筆收款紀錄")
            db.execute("DELETE FROM movements WHERE sale_id=? OR note LIKE ?", (sale_id, f"收款#{sale_id}%"))
            db.execute("DELETE FROM sales WHERE id=?", (sale_id,))
            db.commit()
        self.send_json({"ok": True})

    def clear_collectors(self):
        with conn() as db:
            db.execute(
                """
                INSERT INTO app_settings (key, value)
                VALUES ('collectors_cleared_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (datetime.now().isoformat(timespec="seconds"),),
            )
            db.commit()
        self.send_json({"ok": True})

    def send_csv(self, filename, text):
        body = text.encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def export_summary(self, day):
        summary = daily_summary(day)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["日期", day])
        writer.writerow(["活動", summary["totals"]["events"]])
        writer.writerow(["商品銷售", summary["totals"]["sales_amount"], "捐款", summary["totals"]["donation_amount"], "總收入", summary["totals"]["total_received"], "LINE Pay", summary["totals"]["line_pay_amount"], "現金", summary["totals"]["cash_amount"]])
        writer.writerow([])
        writer.writerow(["商品名稱", "款式", "售出數量", "銷售金額"])
        for row in summary["rows"]:
            writer.writerow([row["name"], row["style"], row["sale_qty"], row["sales_amount"]])
        self.send_csv(f"daily_summary_{day}.csv", out.getvalue())


if __name__ == "__main__":
    setup_db()
    port = int(os.environ.get("BOOTH_APP_PORT", "0"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print("擺攤收款紀錄已啟動")
    print(f"請在瀏覽器使用：{url}")
    if os.environ.get("BOOTH_APP_NO_BROWSER") != "1":
        webbrowser.open(url)
    server.serve_forever()
