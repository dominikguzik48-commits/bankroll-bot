# Running on Windows

Nothing here places a real order. Webull is used for **market data only**. Paper fills
happen inside the bot (`SimBroker`), using live Webull bid/ask prices.

## 1. Install

1. Install Python 3.12 from python.org. Tick **"Add python.exe to PATH"**.
2. Unzip `bankroll-bot` somewhere, e.g. `C:\bankroll-bot`.
3. Open **PowerShell** in that folder and run:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pytest -q
```

If PowerShell blocks `Activate.ps1`, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

## 2. Webull API key

1. In Webull on the web, go to **Center → OpenAPI management**
   (https://www.webull.com/center#openApiManagement). Apply for access and create an app key and secret.
   While you're there, check whether it offers a **paper** account for the API.
2. Store the key in Windows Credential Manager (never in a file, never in chat):

```powershell
python -m bot webull-login
```

3. Check the connection. This is read-only and saves raw responses for checking:

```powershell
python -m bot webull-probe
```

The first call may ask you to approve a login in the Webull app (2FA). The token is saved
in `state\webull_token\`. Send me `state\webull_probe\probe_log.txt` and the `.json`
files. They contain market data only, but look them over first. I'll confirm the field
mapping (bid/ask/delta/open interest) against Webull's real responses.

## 3. Start the paper challenge (only after you approve)

Set `approved_to_start: true` in `config\challenge.yaml`. Until then, `run-day` does nothing.

Schedule the daily run for weekdays at **3:45 PM US/Eastern**. Convert to your local time;
the example below is for a PC set to Eastern time:

```powershell
schtasks /Create /TN "BankrollBot" /TR "C:\bankroll-bot\run_daily.bat" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:45
```

The PC must be awake at that time (Settings → System → Power → Sleep: Never, or use
Task Scheduler's "Wake the computer to run this task"). Holidays are detected
automatically: no SPY bar for today means nothing runs.

## Files to watch

| File | What |
|---|---|
| `logs\alerts.log` | milestones, $2,000 profile alert, kill switch, reconciliation, halts |
| `logs\run_daily.log` | one line per day |
| `reports\graduation.md` | rewritten every Friday |
| `reports\challenge_complete.md` / `challenge_failed.md` | written when the challenge ends |
| `state\paper\` | journal, equity curve, saved state |

**Kill switch:** create an empty file `state\KILL`. The bot flattens everything and opens
nothing until you delete it.
