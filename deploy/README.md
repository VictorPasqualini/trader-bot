# Running this somewhere that does not turn off

The forward test is gated on uptime. Coverage counts every candle close the bot
was not awake for, and the count never expires: 22 misses on record means 220
closes have to accumulate before coverage can reach 90%, which at roughly seven
closes a day is about a month of unbroken running. Every day the machine is off
adds about seven more misses and pushes the target another ten days out. That
makes where the process lives the single largest factor in when the test
finishes — larger than anything in the code.

## Where it can actually run for free

**Oracle Cloud Always Free — the recommendation.** It is the one offer that is
free indefinitely rather than free for a trial, and that gives a machine that
stays up rather than one that sleeps when idle. The always-free allowance is two
AMD `VM.Standard.E2.1.Micro` instances (1 CPU, 1 GB RAM each) plus, when there
is capacity, up to four Ampere ARM cores and 24 GB of RAM, with 200 GB of block
storage. One AMD micro is enough for this bot; the ARM shape is better but is
frequently out of stock in popular regions, so do not wait for it.

Two details decide whether it works:

- **Pick a non-US region.** Binance answers requests from US IP ranges with
  HTTP 451 and no amount of retrying fixes it. `sa-saopaulo-1` or
  `sa-vinhedo-1` are the closest. Your home region is chosen when the tenancy
  is created and cannot be changed afterwards, so this is the one decision that
  is expensive to get wrong.
- **A credit card is required for identity verification**, and the account
  starts on a trial that converts to Always Free when it ends. Make sure the
  instance shapes you keep are the ones labelled *Always Free eligible*, or the
  trial's expiry turns into a bill.

Oracle reclaims always-free compute that has been idle for seven days. A bot
polling the exchange every minute is not idle by any of the measures they use,
so this is not a practical risk here — but it is a real policy, and it is why
leaving the machine up with nothing running is not a way to reserve it.

**Google Cloud's always-free `e2-micro` does not work for this.** The free
instance is only free in `us-west1`, `us-central1` and `us-east1`, all of which
are US IP space, which Binance blocks. The machine would run perfectly and
never place a trade.

**Render, Railway and Fly are not options for an always-on process.** Render's
free web services sleep after fifteen minutes without traffic and its background
workers are paid; Railway's free tier is a one-off credit, not a recurring
allowance; Fly's free allowances were withdrawn. Any of them would produce
exactly the intermittent coverage the gate is designed to catch.

**GitHub Actions on a cron is the only genuinely card-free option**, and it is a
poor fit. Scheduled workflows have a five-minute minimum, are queued rather than
guaranteed, and are routinely delayed by tens of minutes or skipped entirely
under load. On a 4-hour candle a delay of that size still lands inside the
one-bar entry guard, so it would mostly work — but there is no persistent disk,
so the SQLite database would have to be committed back to the repository after
every run, which races with itself and rewrites history on every poll. It trades
a hardware problem for a correctness problem.

**A Raspberry Pi or an old laptop** is not free, but it has no recurring bill,
no card, and no region restriction. If one is already sitting in a drawer it
beats every cloud option on effort.

## Installing on the host

From a checkout on the machine:

    sudo bash deploy/install.sh

It installs the Python toolchain, adds 2 GB of swap (1 GB of RAM is not quite
enough for pandas during a full walk-forward, and an out-of-memory kill mid-poll
is a missed close), creates a service account, builds the virtualenv, enables
NTP, and installs and enables `deploy/pouch.service`. Re-running it redeploys
the code without touching `data/` or `.env`.

Copy your keys over separately — `.env` is gitignored and is deliberately not in
the checkout:

    scp .env <user>@<host>:/tmp/.env
    sudo install -o pouch -g pouch -m 600 /tmp/.env /opt/pouch/.env && rm /tmp/.env
    sudo systemctl restart pouch

The bot restarts itself: `bot/api.py`'s startup hook re-enables it whenever the
saved config has it enabled, so a reboot costs one poll interval.

## Reaching the dashboard

The service binds to `127.0.0.1` on purpose. There is no login screen, and the
same interface that shows the equity curve also starts the bot, edits the book
and places orders. Forward the port over SSH instead of opening it:

    ssh -L 8777:127.0.0.1:8777 <user>@<host>

then open `http://127.0.0.1:8777` locally. If the dashboard is ever to be
exposed directly, it needs authentication first, not a firewall rule.

## Moving an existing run

The database is the whole history — orders, positions, equity snapshots, the
frozen expectations and the coverage ticks. Copy it while the service is
stopped, or SQLite may hand you a file mid-write:

    sudo systemctl stop pouch
    scp /opt/pouch/data/trader.db <somewhere safe>

Once the move is done and the bot is running on the new host, reset the coverage
baseline **once**, at that moment:

    curl -X POST http://127.0.0.1:8777/api/coverage/baseline \
         -H 'content-type: application/json' -d '{}'

That sets aside the closes missed under the old arrangement so the percentage
measures the new one. It is honest exactly once — at a real change of
deployment. Calling it again because the number is unflattering makes the
number measure nothing at all, which is why there is no button for it in the
interface.

## Before it holds real money

Everything above is written for the testnet keys. A host that holds live
credentials needs more than this: disk encryption, no shared account, key
rotation, and an answer to what happens if the machine is compromised while a
position is open. `deploy/install.sh` locks `.env` to mode 600 and nothing else.
