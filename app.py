import base64
import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any

import requests
from flask import Flask, Response, request
from openai import OpenAI


app = Flask(__name__)


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


OPENAI_CLIENT = OpenAI(api_key=env("OPENAI_API_KEY", ""))
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4o-mini")

HARDCODED_ACCURATE_ACCESS_TOKEN = "2550cc28-6044-4dc1-ba29-b9746658c3b2"
HARDCODED_ACCURATE_REFRESH_TOKEN = "30955b51-bace-42ca-ac8d-3753e4f4c96d"

ACCURATE_ACCESS_TOKEN = os.getenv("ACCURATE_ACCESS_TOKEN", HARDCODED_ACCURATE_ACCESS_TOKEN)
ACCURATE_CLIENT_ID = env("ACCURATE_CLIENT_ID")
ACCURATE_CLIENT_SECRET = env("ACCURATE_CLIENT_SECRET")
ACCURATE_REFRESH_TOKEN = env("ACCURATE_REFRESH_TOKEN", HARDCODED_ACCURATE_REFRESH_TOKEN)
ACCURATE_DB_ID = env("ACCURATE_DB_ID")
ACCURATE_ACCOUNT_BASE_URL = env("ACCURATE_ACCOUNT_BASE_URL", "https://account.accurate.id")
DEFAULT_WAREHOUSE_NAME = env("DEFAULT_STOCK_WAREHOUSE_NAME", "Utama")


class AccurateClient:
    def __init__(self) -> None:
        self.access_token: str | None = ACCURATE_ACCESS_TOKEN or HARDCODED_ACCURATE_ACCESS_TOKEN or None
        self.session_id: str | None = None
        self.host: str | None = None

    def _basic_auth_header(self) -> str:
        token = base64.b64encode(f"{ACCURATE_CLIENT_ID}:{ACCURATE_CLIENT_SECRET}".encode()).decode()
        return f"Basic {token}"

    def refresh_access_token(self) -> str:
        response = requests.post(
            f"{ACCURATE_ACCOUNT_BASE_URL}/oauth/token",
            headers={"Authorization": self._basic_auth_header()},
            data={"grant_type": "refresh_token", "refresh_token": ACCURATE_REFRESH_TOKEN},
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text[:500]
            raise RuntimeError(f"Accurate token refresh failed: {response.status_code} {body}") from exc
        data = response.json()
        self.access_token = data["access_token"]
        return str(self.access_token)

    def open_db(self) -> None:
        if not self.access_token:
            self.refresh_access_token()
        response = requests.get(
            f"{ACCURATE_ACCOUNT_BASE_URL}/api/open-db.do",
            params={"id": ACCURATE_DB_ID},
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=30,
        )
        if response.status_code == 401 and ACCURATE_ACCESS_TOKEN and self.access_token == ACCURATE_ACCESS_TOKEN:
            self.refresh_access_token()
            response = requests.get(
                f"{ACCURATE_ACCOUNT_BASE_URL}/api/open-db.do",
                params={"id": ACCURATE_DB_ID},
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=30,
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text[:500]
            raise RuntimeError(f"Accurate open-db failed: {response.status_code} {body}") from exc
        data = response.json()
        self.host = data["host"]
        self.session_id = data["session"]

    def api_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.host or not self.session_id or not self.access_token:
            self.open_db()
        response = requests.get(
            f"{self.host}/accurate/api{path}",
            params=params or {},
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "X-Session-ID": self.session_id,
            },
            timeout=60,
        )
        if response.status_code == 401:
            self.host = None
            self.session_id = None
            self.refresh_access_token()
            self.open_db()
            response = requests.get(
                f"{self.host}/accurate/api{path}",
                params=params or {},
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "X-Session-ID": self.session_id,
                },
                timeout=60,
            )
        response.raise_for_status()
        return response.json()


def twiml(text: str) -> Response:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return Response(f"<Response><Message>{safe}</Message></Response>", mimetype="text/xml")


def parse_relative_range(value: str | None) -> tuple[str, str]:
    today = date.today()
    if not value:
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat()
    text = value.lower()
    if "3 bulan" in text or "90" in text:
        start = today - timedelta(days=90)
        return start.isoformat(), today.isoformat()
    if "bulan ini" in text:
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat()
    start = today.replace(day=1)
    return start.isoformat(), today.isoformat()


def parse_date_safe(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def score_match(query: str, candidate: str) -> int:
    q = normalize_text(query)
    c = normalize_text(candidate)
    if not q or not c:
        return 0
    if q == c:
        return 1000
    score = 0
    if q in c:
        score += 500
    q_tokens = q.split()
    c_tokens = set(c.split())
    score += sum(100 for token in q_tokens if token in c_tokens)
    if q.replace(" ", "") in c.replace(" ", ""):
        score += 250
    return score


def in_range(date_value: Any, start_iso: str, end_iso: str) -> bool:
    d = parse_date_safe(date_value)
    if d is None:
        return False
    start = parse_date_safe(start_iso)
    end = parse_date_safe(end_iso)
    if start is None or end is None:
        return False
    return start <= d <= end


def list_items(term: str) -> list[dict[str, Any]]:
    client = AccurateClient()
    term_lower = term.lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for page in range(1, 21):
        data = client.api_get("/item/list.do", {"fields": "id,no,name,itemType", "sp.page": page, "sp.pageSize": 200})
        rows = data.get("d", [])
        for row in rows:
            name = str(row.get("name", ""))
            no = str(row.get("no", ""))
            score = max(score_match(term, name), 900 if term_lower == no.lower() else 0, score_match(term, no))
            if score > 0:
                scored.append((score, row))
        sp = data.get("sp") or {}
        if page >= int(sp.get("pageCount") or 1):
            break
    scored.sort(key=lambda x: (-x[0], str(x[1].get("name", ""))))
    return [row for _, row in scored[:10]]


def get_item_by_code(item_no: str) -> dict[str, Any]:
    matches = list_items(item_no)
    for row in matches:
        if str(row.get("no", "")).lower() == str(item_no).lower():
            return row
    return {}


def fast_path_response(user_message: str) -> str | None:
    text = user_message.strip()
    norm = normalize_text(text)

    match_code_stock = re.search(r"(?:stok|stock).*(?:kode )?(\d{3,})", norm)
    if match_code_stock:
        code = match_code_stock.group(1)
        stock = get_item_stock(code, DEFAULT_WAREHOUSE_NAME)
        qty = float((stock.get("d") or {}).get("availableStock", 0) or 0)
        item = get_item_by_code(code)
        name = item.get("name") or f"kode {code}"
        return f"Stok barang {name} di gudang {DEFAULT_WAREHOUSE_NAME} adalah {qty:.0f} unit."

    match_code_name = re.search(r"nama barang.*(?:kode )?(\d{3,})", norm)
    if match_code_name:
        code = match_code_name.group(1)
        item = get_item_by_code(code)
        if item:
            return f"Nama barang dengan kode {code} adalah {item.get('name')}."
        return f"Barang dengan kode {code} tidak ditemukan."

    if "stok barang" in norm or norm.startswith("berapa stok"):
        query = re.sub(r"^(berapa stok barang|berapa stok|stok barang|stok)\s*", "", text, flags=re.IGNORECASE).strip()
        if query:
            matches = list_items(query)
            if matches:
                best = matches[0]
                code = str(best.get("no", ""))
                stock = get_item_stock(code, DEFAULT_WAREHOUSE_NAME)
                qty = float((stock.get("d") or {}).get("availableStock", 0) or 0)
                return f"Stok barang {best.get('name')} di gudang {DEFAULT_WAREHOUSE_NAME} adalah {qty:.0f} unit."
            return f"Barang dengan nama {query} tidak ditemukan."

    return None


def get_item_stock(item_no: str, warehouse_name: str | None = None) -> dict[str, Any]:
    client = AccurateClient()
    params = {"no": item_no, "warehouseName": DEFAULT_WAREHOUSE_NAME}
    return client.api_get("/item/get-stock.do", params)


def get_sell_price(item_no: str) -> dict[str, Any]:
    client = AccurateClient()
    return client.api_get("/item/get-selling-price.do", {"no": item_no})


def get_buy_price(item_no: str) -> dict[str, Any]:
    client = AccurateClient()
    return client.api_get("/item/vendor-price.do", {"no": item_no})


def get_sales_invoices() -> dict[str, Any]:
    client = AccurateClient()
    return client.api_get("/sales-invoice/list.do", {"sp.page": 1, "sp.pageSize": 500})


def get_purchase_invoices() -> dict[str, Any]:
    client = AccurateClient()
    return client.api_get("/purchase-invoice/list.do", {"sp.page": 1, "sp.pageSize": 500})


def list_stock(limit: int = 10) -> dict[str, Any]:
    client = AccurateClient()
    data = client.api_get("/item/list-stock.do", {"sp.page": 1, "sp.pageSize": max(1, min(limit, 100))})
    return {"items": data.get("d", [])}


def list_stock_adv(limit: int = 10, page: int = 1, warehouse_name: str | None = None) -> dict[str, Any]:
    client = AccurateClient()
    params: dict[str, Any] = {"sp.page": max(1, page), "sp.pageSize": max(1, min(limit, 100)), "warehouseName": DEFAULT_WAREHOUSE_NAME}
    data = client.api_get("/item/list-stock.do", params)
    return {"items": data.get("d", []), "page": params["sp.page"], "page_size": params["sp.pageSize"]}


def list_low_stock(limit: int = 10, threshold: float = 0) -> dict[str, Any]:
    client = AccurateClient()
    data = client.api_get("/item/list-stock.do", {"sp.page": 1, "sp.pageSize": 500})
    rows = data.get("d", [])
    filtered = [r for r in rows if float(r.get("quantity", 0) or 0) <= threshold]
    filtered.sort(key=lambda x: float(x.get("quantity", 0) or 0))
    return {"items": filtered[:limit]}


def list_low_stock_adv(limit: int = 10, threshold: float = 0, warehouse_name: str | None = None, page: int = 1) -> dict[str, Any]:
    client = AccurateClient()
    params: dict[str, Any] = {"sp.page": max(1, page), "sp.pageSize": 500, "warehouseName": DEFAULT_WAREHOUSE_NAME}
    data = client.api_get("/item/list-stock.do", params)
    rows = data.get("d", [])
    filtered = [r for r in rows if float(r.get("quantity", 0) or 0) <= threshold]
    filtered.sort(key=lambda x: float(x.get("quantity", 0) or 0))
    return {"items": filtered[:limit], "threshold": threshold, "warehouse_name": DEFAULT_WAREHOUSE_NAME}


def customer_purchase_history(customer_name: str, date_range_text: str | None = None, limit: int = 10) -> dict[str, Any]:
    start, end = parse_relative_range(date_range_text)
    invoices = collect_sales_details()
    name_lower = customer_name.lower()
    matched = []
    for row in invoices:
        cust = str((row.get("customer") or {}).get("name", "") or row.get("customerName", "") or row.get("customer", "")).lower()
        if name_lower not in cust:
            continue
        trans_date = row.get("transDate") or row.get("invoiceDate")
        if trans_date and not in_range(trans_date, start, end):
            continue
        matched.append(
            {
                "number": row.get("number") or row.get("invoiceNo"),
                "transDate": trans_date,
                "customerName": (row.get("customer") or {}).get("name") or row.get("customerName") or row.get("customer"),
                "totalAmount": row.get("totalAmount") or row.get("amount") or 0,
                "outstandingAmount": row.get("outstandingAmount") or row.get("balance") or 0,
            }
        )
    return {"from_date": start, "to_date": end, "rows": matched[:limit], "count": len(matched)}


def sales_summary(date_range_text: str | None = None, limit: int = 10) -> dict[str, Any]:
    start, end = parse_relative_range(date_range_text)
    invoices = collect_sales_details()
    rows = []
    total = 0.0
    for row in invoices:
        trans_date = row.get("transDate") or row.get("invoiceDate")
        if trans_date and not in_range(trans_date, start, end):
            continue
        amount = float(row.get("totalAmount", 0) or row.get("amount", 0) or 0)
        total += amount
        rows.append({
            "number": row.get("number") or row.get("invoiceNo"),
            "transDate": trans_date,
            "customerName": (row.get("customer") or {}).get("name") or row.get("customerName") or row.get("customer"),
            "totalAmount": amount,
        })
    rows.sort(key=lambda x: x.get("transDate") or "", reverse=True)
    return {"from_date": start, "to_date": end, "count": len(rows), "total_amount": total, "rows": rows[:limit]}


def piutang_due_list(date_range_text: str | None = None, customer_name: str | None = None, limit: int = 10) -> dict[str, Any]:
    start, end = parse_relative_range(date_range_text)
    invoices = collect_sales_details()
    name_lower = (customer_name or "").lower().strip()
    rows = []
    for row in invoices:
        outstanding = float(row.get("primeOwing", 0) or row.get("outstandingAmount", 0) or row.get("balance", 0) or 0)
        if not bool(row.get("outstanding")) or outstanding <= 0:
            continue
        cust = str((row.get("customer") or {}).get("name", "") or row.get("customerName", "") or row.get("customer", ""))
        if name_lower and name_lower not in cust.lower():
            continue
        due_date = row.get("dueDate") or row.get("maturityDate") or row.get("transDate")
        if due_date and not in_range(due_date, start, end):
            continue
        rows.append({
            "number": row.get("number") or row.get("invoiceNo"),
            "customerName": cust,
            "dueDate": due_date,
            "outstandingAmount": outstanding,
        })
    rows.sort(key=lambda x: parse_date_safe(x.get("dueDate")) or date.max)
    return {"from_date": start, "to_date": end, "rows": rows[:limit], "count": len(rows)}


def hutang_due_list(date_range_text: str | None = None, supplier_name: str | None = None, limit: int = 10) -> dict[str, Any]:
    start, end = parse_relative_range(date_range_text)
    invoices = collect_purchase_details()
    name_lower = (supplier_name or "").lower().strip()
    rows = []
    for row in invoices:
        outstanding = float(row.get("primeOwing", 0) or row.get("outstandingAmount", 0) or row.get("balance", 0) or 0)
        if not bool(row.get("outstanding")) or outstanding <= 0:
            continue
        vendor = str((row.get("vendor") or {}).get("name", "") or row.get("vendorName", "") or row.get("supplierName", "") or row.get("vendor", ""))
        if name_lower and name_lower not in vendor.lower():
            continue
        due_date = row.get("dueDate") or row.get("maturityDate") or row.get("transDate")
        if due_date and not in_range(due_date, start, end):
            continue
        rows.append({
            "number": row.get("number") or row.get("invoiceNo"),
            "supplierName": vendor,
            "dueDate": due_date,
            "outstandingAmount": outstanding,
        })
    rows.sort(key=lambda x: parse_date_safe(x.get("dueDate")) or date.max)
    return {"from_date": start, "to_date": end, "rows": rows[:limit], "count": len(rows)}


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_item",
            "description": "Find item by code or name and return best matches.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_by_code",
            "description": "Get item master data by exact item code.",
            "parameters": {"type": "object", "properties": {"item_no": {"type": "string"}}, "required": ["item_no"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stock",
            "description": "List items with stock quantities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "page": {"type": "integer"},
                    "warehouse_name": {"type": "string"}
                }
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_low_stock",
            "description": "List low-stock items using a threshold and result limit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "threshold": {"type": "number"},
                    "warehouse_name": {"type": "string"},
                    "page": {"type": "integer"}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "customer_purchase_history",
            "description": "Get customer sales invoice history for a date range like 'bulan ini' or '3 bulan'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "date_range_text": {"type": "string"},
                    "limit": {"type": "integer"},
                    "page": {"type": "integer"},
                },
                "required": ["customer_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sales_summary",
            "description": "Get sales summary for a date range, optionally limiting returned sample rows.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range_text": {"type": "string"},
                    "limit": {"type": "integer"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_item_stock",
            "description": "Get available stock for a specific item number, optionally by warehouse.",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_no": {"type": "string"},
                    "warehouse_name": {"type": "string"},
                },
                "required": ["item_no"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sell_price",
            "description": "Get sell price for an item number.",
            "parameters": {"type": "object", "properties": {"item_no": {"type": "string"}}, "required": ["item_no"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_buy_price",
            "description": "Get buy/vendor price for an item number.",
            "parameters": {"type": "object", "properties": {"item_no": {"type": "string"}}, "required": ["item_no"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_piutang_summary",
            "description": "Get receivables summary for a date range text like 'bulan ini' or '3 bulan'.",
            "parameters": {"type": "object", "properties": {"date_range_text": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hutang_summary",
            "description": "Get payables summary for a date range text like 'bulan ini' or '3 bulan'.",
            "parameters": {"type": "object", "properties": {"date_range_text": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "piutang_due_list",
            "description": "List receivables due within a date range, optionally filtered by customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range_text": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "limit": {"type": "integer"},
                    "page": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hutang_due_list",
            "description": "List payables due within a date range, optionally filtered by supplier.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_range_text": {"type": "string"},
                    "supplier_name": {"type": "string"},
                    "limit": {"type": "integer"},
                    "page": {"type": "integer"},
                },
            },
        },
    },
]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "find_item":
        return {"matches": list_items(arguments["query"])}
    if name == "get_item_by_code":
        return get_item_by_code(arguments["item_no"])
    if name == "get_item_stock":
        return get_item_stock(arguments["item_no"], arguments.get("warehouse_name") or DEFAULT_WAREHOUSE_NAME)
    if name == "get_sell_price":
        return get_sell_price(arguments["item_no"])
    if name == "get_buy_price":
        return get_buy_price(arguments["item_no"])
    if name == "list_stock":
        return list_stock_adv(int(arguments.get("limit") or 10), int(arguments.get("page") or 1), arguments.get("warehouse_name") or DEFAULT_WAREHOUSE_NAME)
    if name == "list_low_stock":
        return list_low_stock_adv(int(arguments.get("limit") or 10), float(arguments.get("threshold") or 0), arguments.get("warehouse_name") or DEFAULT_WAREHOUSE_NAME, int(arguments.get("page") or 1))
    if name == "customer_purchase_history":
        return customer_purchase_history(arguments["customer_name"], arguments.get("date_range_text"), int(arguments.get("limit") or 10))
    if name == "sales_summary":
        return sales_summary(arguments.get("date_range_text"), int(arguments.get("limit") or 10))
    if name == "get_piutang_summary":
        start, end = parse_relative_range(arguments.get("date_range_text"))
        data = collect_sales_details()
        unpaid = [row for row in data if bool(row.get("outstanding")) and float(row.get("primeOwing", 0) or row.get("outstandingAmount", 0) or row.get("balance", 0) or 0) > 0 and in_range(row.get("dueDate") or row.get("transDate"), start, end)]
        total = sum(float(row.get("primeOwing", 0) or row.get("outstandingAmount", 0) or 0) for row in unpaid)
        return {"from_date": start, "to_date": end, "count": len(unpaid), "total_outstanding": total}
    if name == "get_hutang_summary":
        start, end = parse_relative_range(arguments.get("date_range_text"))
        data = collect_purchase_details()
        unpaid = [row for row in data if bool(row.get("outstanding")) and float(row.get("primeOwing", 0) or row.get("outstandingAmount", 0) or row.get("balance", 0) or 0) > 0 and in_range(row.get("dueDate") or row.get("transDate"), start, end)]
        total = sum(float(row.get("primeOwing", 0) or row.get("outstandingAmount", 0) or 0) for row in unpaid)
        return {"from_date": start, "to_date": end, "count": len(unpaid), "total_outstanding": total}
    if name == "piutang_due_list":
        return piutang_due_list(arguments.get("date_range_text"), arguments.get("customer_name"), int(arguments.get("limit") or 10))
    if name == "hutang_due_list":
        return hutang_due_list(arguments.get("date_range_text"), arguments.get("supplier_name"), int(arguments.get("limit") or 10))
    raise ValueError(f"Unknown tool: {name}")


SYSTEM_PROMPT = """
You are a WhatsApp business assistant for Accurate Online data.
Use tools when needed. Prefer concise Bahasa Indonesia answers.
If item name is ambiguous, ask a short clarification question.
For stock queries, always use warehouse 'Utama'. Ignore any user-requested warehouse.
For hutang/piutang with no date range, default to current month.
Never invent stock or price numbers.
When listing money values, format them in Rupiah style briefly.
When tool output is empty, clearly say data tidak ditemukan.
""".strip()


def run_agent(user_message: str) -> str:
    quick = fast_path_response(user_message)
    if quick:
        return quick

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    for _ in range(6):
        response = OPENAI_CLIENT.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = call_tool(tc.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            continue

        return (msg.content or "Maaf, saya belum bisa memproses permintaan itu.").strip()

    return "Maaf, saya gagal menyelesaikan permintaan. Silakan coba lagi."


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/twilio/whatsapp")
def twilio_whatsapp() -> Response:
    user_message = request.form.get("Body", "").strip()
    if not user_message:
        return twiml("Pesan kosong.")
    try:
        reply = run_agent(user_message)
        return twiml(reply)
    except Exception as exc:
        return twiml(f"Maaf, terjadi kesalahan: {str(exc)[:200]}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
def get_sales_invoice_detail(invoice_id: int) -> dict[str, Any]:
    client = AccurateClient()
    return client.api_get("/sales-invoice/detail.do", {"id": invoice_id}).get("d", {})


def get_purchase_invoice_detail(invoice_id: int) -> dict[str, Any]:
    client = AccurateClient()
    return client.api_get("/purchase-invoice/detail.do", {"id": invoice_id}).get("d", {})


def collect_sales_details(limit_pages: int = 5) -> list[dict[str, Any]]:
    details = []
    client = AccurateClient()
    for page in range(1, limit_pages + 1):
        data = client.api_get("/sales-invoice/list.do", {"sp.page": page, "sp.pageSize": 100})
        for row in data.get("d", []):
            if row.get("id") is not None:
                details.append(get_sales_invoice_detail(int(row["id"])))
        sp = data.get("sp") or {}
        if page >= int(sp.get("pageCount") or 1):
            break
    return details


def collect_purchase_details(limit_pages: int = 5) -> list[dict[str, Any]]:
    details = []
    client = AccurateClient()
    for page in range(1, limit_pages + 1):
        data = client.api_get("/purchase-invoice/list.do", {"sp.page": page, "sp.pageSize": 100})
        for row in data.get("d", []):
            if row.get("id") is not None:
                details.append(get_purchase_invoice_detail(int(row["id"])))
        sp = data.get("sp") or {}
        if page >= int(sp.get("pageCount") or 1):
            break
    return details
