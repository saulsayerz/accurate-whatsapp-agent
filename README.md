# Flask WhatsApp Agent

## What it does

- exposes a Twilio WhatsApp webhook at `/twilio/whatsapp`
- refreshes Accurate OAuth token
- opens Accurate DB session
- lets the LLM choose tools and tool parameters
- returns TwiML so Twilio sends the final WhatsApp reply

## Tools currently included

- `find_item`
- `get_item_stock`
- `get_sell_price`
- `get_buy_price`
- `list_stock`
- `list_low_stock`
- `customer_purchase_history`
- `get_piutang_summary`
- `get_hutang_summary`
- `piutang_due_list`
- `hutang_due_list`

## Local run

1. Create `.env` from `.env.example`
2. Install deps:

```bat
pip install -r requirements.txt
```

3. Run:

```bat
python app.py
```

4. Twilio webhook URL:

```text
https://YOUR-DOMAIN/twilio/whatsapp
```

## Deploy

- Render: use `render.yaml`
- Any VPS: run `gunicorn app:app`

## Important

- Rotate exposed Accurate/Twilio/OpenAI secrets after deployment is stable.
- This version is read-only.
