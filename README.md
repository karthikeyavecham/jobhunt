# JobHunt

A self-hosted job aggregator. It fetches postings from configurable sources,
filters them to a region you can actually work in, scores each one against your
profile with a local LLM, pushes the good ones to Slack as interactive cards,
and drafts tailored CVs and cover letters as Word documents.

Built to run unattended on a Raspberry Pi with the model served from a desktop
on the same network, but it runs fine on one machine.

**It is yours to point at yourself.** Nothing about any particular person is
compiled in: your CV, contact details, commute region and the things a model
must never overstate about you all live in the profile, editable in the app.

---

## What it does

```
sources/*.py   fetch postings          one adapter per board, no database access
dedup.py       identity rules          pure functions, heavily unit-tested
ingest.py      the only writer         owns every dedup decision
filters.py     stage-0 relevance       region and age, before any model runs
scoring.py     stage-1 fit             one LLM call per posting, scored once
generation.py  CV and cover letter     the model plans, the code renders
slackbot.py    cards and actions       Socket Mode, no inbound port
scheduler.py   the unattended cycle    fetch, score, notify, every few hours
```

Two filtering stages exist because they cost different amounts. Stage-0 is
string matching: it throws away everything outside your region before a single
token is spent. Stage-1 is the model, and only relevant postings reach it.

Postings are **marked, not dropped**, so exclusions stay auditable, reverse
cleanly when you change the region, and can be re-evaluated in place.

## Getting started

Requires **Python 3.13** and an OpenAI-compatible local model server — [LM
Studio](https://lmstudio.ai) or Ollama. A 27B-class instruct model is enough;
scoring is a structured-output task, not a reasoning one.

```bash
python3.13 -m venv venv
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m jobhunt.app
```

Then open <http://127.0.0.1:5002> and go to **Profile**.

### Your profile is the first thing to set

Everything downstream reads it, and until it is filled in the app scores
postings against placeholder text. Two ways to set it:

- **The Profile page** — contact details, career summary, skills, preferences,
  the languages line printed on your CV, and the constraints below.
- **`profile_local.json`** — copy `profile.example.json` to that name and fill
  it in. Read once on first run, and gitignored, so a personal profile never
  reaches a repository. Anything saved in the app wins over it.

The section worth explaining is **"Never overstate"**. It is injected verbatim
into every CV and cover letter prompt as hard rules. Write plain statements:

```
- German is A2. Never imply a higher level anywhere.
- Has mentored engineers but has never had direct reports.
- Never mention a notice period or departure date.
```

Models are agreeable by default, and a posting asking for fluent German will
pull a CV toward claiming it. These rules are what stop that, and they are
yours rather than the app's because nobody else can write them for you.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Anything else requires `JOBHUNT_PASSWORD`. |
| `PORT` | `5002` | Listen port. |
| `JOBHUNT_PASSWORD` | — | Required before the app binds to a non-loopback address. |
| `JOBHUNT_SECRET_KEY` | random | Flask session signing key. |
| `JOBHUNT_ROLE` | `web` | `production` starts the scheduler and Slack. Exactly one host may set it. |
| `JOBHUNT_LOG_LEVEL` | `INFO` | Python log level. |

The app **refuses to start** on a non-loopback address without
`JOBHUNT_PASSWORD`: the database holds API tokens and personal data. The file
is chmod 600 on creation.

**Exactly one instance may own the schedule.** Two would mean two schedulers
double-fetching and two Slack connections splitting button clicks between two
databases, so owning them is opt-in via `JOBHUNT_ROLE=production`. Running the
app anywhere else gives a read-only web UI. If nothing is being fetched, check
that variable first — the log says `role=web` when it is missing.

## Sources

| Adapter | Auth | Notes |
|---|---|---|
| `arbeitnow` | none | Public JSON API. Europe-wide, so location filtering happens locally. |
| `arbeitsagentur` | static key | The German federal employment agency — the largest German dataset. Searches server-side by job title and region. |
| `greenhouse` `lever` `smartrecruiters` `personio` `recruitee` | none | Per-company boards, added by slug from the Sources page. |
| `manual` | none | Paste a URL or the posting text. |

**Ashby is deliberately absent.** Its public API returns titles and locations
but no description, and a posting without a description cannot be scored
meaningfully — including it would manufacture exactly the noise the filters
exist to remove.

Company slugs cannot be guessed, so the **Test** button reports description
coverage as well as reachability. Discovering that a board returns no
descriptions at configuration time beats discovering it weeks later as a run of
odd scores.

Boards that forbid automated fetching (LinkedIn, StepStone, Indeed) are not
adapters and will not be. Use the manual import for those: the form refuses to
fetch those hosts and asks for pasted text instead, so the posting still enters
the same pipeline without putting your account at risk.

## Scoring

One call per posting, scored once, keyed on the job. At a three-hour cycle that
keeps the cost proportional to genuinely new work.

Three honesty rules are enforced **in code rather than trusted to the prompt**,
because a prompt is a request and these are requirements:

- A language or salary claim without a verbatim quote from the posting is
  downgraded to "not stated". Observed directly: a model returned a required
  German level for postings that never mention German.
- Score and verdict must agree with the rubric bands. When they disagree the
  score wins and the verdict is relabelled — the score is the model's last and
  best-considered output, and clamping it into the verdict's band actively
  destroyed good matches.
- Structured signals from the source outrank the model's reading of the prose.

The reasoning fields come **first** in the output schema, so the model states
its evidence before committing to a number. With the score first, one model
returned `fit_score: 0` alongside `verdict: strong`.

### Parallel scoring

`scoring.concurrency` sets how many scoring requests are in flight at once.
Whether that helps depends entirely on the model server — if it served requests
one at a time, parallel callers would just queue. LM Studio batches them, and
the gain is real. Measured on a 27B model:

| In parallel | Throughput | Per request |
|---|---|---|
| 1 | 2.2 jobs/min | 27s |
| 2 | 3.9 jobs/min | 31s |
| 4 | 5.1 jobs/min | 37s |
| 8 | 6.1 jobs/min | 53s |

Three or four is the sweet spot: past that, throughput flattens while latency
keeps climbing. Default is 1, because the model usually runs on a machine
someone is using.

### The Scoring page

Shows what has been scored, the backlog, and a breakdown **per day**, with a
button to work through one day on demand. That exists because the queue is
ordered newest-first and each cycle takes a fixed batch — so when daily intake
exceeds what the cycles can score, the newest postings consume the whole budget
and older days are never reached, no matter how long you wait.

## Document generation

**The model plans; the code renders.** The LLM returns structured JSON — which
bullets, in what order, under which heading — and never emits a `.docx` or
picks a font. Formatting is therefore always correct, and a bad generation can
only produce bad *wording*, never a broken document.

Four guarantees are enforced in code:

- Contact details are injected from your profile. The model never gets the
  chance to invent a phone number.
- The languages line is copied verbatim from your profile, whatever the model
  wrote.
- Generated text is scanned for employer-internal terms before delivery; a hit
  blocks the document rather than warning after the fact.
- A double-click cannot produce two versions and two model calls.

The prompts are editable at **Configuration → Writing prompts**, with the
composed prompt shown exactly as the model receives it. The prompt is the main
quality lever in the app: the same profile and posting produce a merely
adequate CV or a good one depending almost entirely on what is asked for.

## Slack

Socket Mode, so there is no inbound port and no public URL — the app makes an
outbound WebSocket connection and works from behind a home router untouched.

Payloads arrive over an authenticated socket, but any workspace member can
click a button, so every action and every slash command checks the user against
an allowlist. An empty allowlist permits nobody.

```
/jobhunt status   pipeline summary
/jobhunt top      best unactioned matches
/jobhunt titles   the job titles being searched
/jobhunt run      fetch, score and notify now
```

## The application tracker

`/applications` is the part still in daily use once the fetching has done its
job: the pipeline from `shortlisted` through `applied`, `screening`,
`interview` and `offer`, with `rejected` and `withdrawn` as the ways out.

Export to `.xlsx` or JSON, and import an existing spreadsheet — headers are
matched across common spellings, dates parse in both `15.06.2026` and
`2026-06-15` form, and anything unrecognised is **reported, not dropped
silently**.

Two quiet rules worth knowing: blank means *unchanged*, so clearing a field
needs a deliberate edit rather than an empty submit; and reaching a stage past
"applied" backfills today's date, except on import, where a row with no date is
history and stamping it with today would reset every follow-up clock.

## Tests

```bash
./venv/bin/python -m unittest discover -s tests -t ..
```

529 tests. They never touch the network — adapters run against saved real
responses in `tests/fixtures/`, and ingest tests use stub adapters against a
temporary database.

Green tests are not the whole story. Several bugs have shipped past a green
suite and been caught another way: a duplicate aggregator source and invented
application dates by clicking through the UI, a four-minor-version Python split
between test and production by checking the running host, and a misplaced
`__main__` guard that was silently skipping the CSRF tests. Run the suite, then
use the app, then look at what production is actually doing.

## Deployment

`deploy-to-pi.sh` syncs the code to a Raspberry Pi, pulls a fresh database
backup back first, and runs the installer. Override the target with `PI_HOST`,
`PI_USER`, `PI_KEY` and `REMOTE_DIR`.

The database is copied only on the **first** deploy. It carries the scores, the
tracker, the secrets and the "already notified" markers, so later deploys leave
it alone unless `--overwrite-db` is passed, which backs up first.

Housekeeping runs nightly: a consistent snapshot via SQLite's backup API (a
plain file copy under WAL can be inconsistent), keeping the newest few, and
every deploy pulls the newest backup off the device.

## Security posture

A LAN-only personal app, one user, no inbound exposure.

- **HTTP Basic auth** whenever `JOBHUNT_PASSWORD` is set, with the password in
  a chmod-600 env file.
- **CSRF**: browsers attach Basic credentials automatically, so any website you
  visit could otherwise fire authenticated POSTs at the app. State-changing
  requests whose `Origin` names a different host are rejected.
- **SSRF**: the manual import resolves hostnames and rejects loopback, private,
  link-local and reserved addresses — without which the form would happily
  fetch the app's own config page.
- **Secrets** live in the database (chmod 600) or environment variables, which
  always win. Nothing is committed.
- Accepted trade-offs: Basic auth over plain HTTP on a home network, and
  secrets at rest in a single-user SQLite file.

## Licence

No licence yet — all rights reserved. Open an issue if you want to use it.
