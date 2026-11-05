# Kalshi Clone Backend

Small Django API wrapper around Kalshi public market data.

## Run

```powershell
..\kalshibot\Scripts\python.exe manage.py runserver 8000
```

## Endpoints

```txt
GET /api/kalshi/health/
GET /api/kalshi/miami-temperature/
GET /api/kalshi/miami-temperature/?market_ticker=KXHIGHMIA-26JUN15-B93.5
GET /api/kalshi/miami-temperature/stream/
GET /api/kalshi/nws/dsm/            (KMIA ASOS Daily Summary Message, decoded + raw, plus the high the models are using)
GET /api/kalshi/paper/state/
POST /api/kalshi/paper/order/
POST /api/kalshi/paper/reset/
GET  /api/kalshi/autotrader/state/    (paper account, positions, trades, bot cycles, schedule)
POST /api/kalshi/autotrader/toggle/   {"enabled": true|false}
POST /api/kalshi/autotrader/reset/    (restore $10,000, clear positions/trades/bot log)
```

## Kalshi WebSocket credentials

Copy `.env.example` to `.env` and fill in your Kalshi API key id and private key path.

```powershell
Copy-Item .env.example .env
```

For fast KMIA current temperature and recorded high, also set:

```env
SYNOPTIC_TOKEN=your_synoptic_token
SYNOPTIC_STATION=KMIA
CRON_REFRESH_SECRET=choose-a-long-random-string
```

Do not put Kalshi credentials in the frontend.

## Paper-trading bot

Started with the other background tasks (`RUN_BACKGROUND_TASKS`, single worker). Every 15 minutes
from 12:30 PM to 3:30 PM ET it buys 100 contracts of YES on the highest-probability live bucket and
100 contracts of NO on every other live bucket (`kalshi_api/algorithm.py`, `kalshi_api/autotrader.py`).
It does not compare the model with Kalshi's prices; they are only the fill price (a leg is skipped if it
has no ask or the ask is above 98¢). It never sells; positions settle from Kalshi's resolved result.
Starting balance is $10,000.
