# Causematch: architecture and features

This document has two readers in mind: a developer joining the project, and a technical partner who wants to know what the app does and how it is built.

- **Part 1 is a 10-minute tour.** It follows the [C4 model](https://c4model.com): first the people and outside services around Causematch, then the running pieces, then the parts inside the app. Each section has one picture and a few short paragraphs, and it ends with the handful of ideas that make the app work.
- **Part 2 is the reference.** Routes, modules, tables, flows, ranking weights, security rules, environments and tests, with file paths for everything.

File paths are relative to the repo root. "Live" means running in the password-protected preview on Cloud Run. Production is not deployed yet.

## Contents

**Part 1: The tour**

1. [The idea in one picture](#1-the-idea-in-one-picture)
2. [Level 1: system context](#2-level-1-system-context)
3. [Level 2: containers](#3-level-2-containers)
4. [Level 3: components inside the app](#4-level-3-components-inside-the-app)
5. [Key ideas](#5-key-ideas)

**Part 2: Reference**

6. [Features by role](#6-features-by-role)
7. [Request pipeline](#7-request-pipeline)
8. [Module map](#8-module-map)
9. [Data model](#9-data-model)
10. [Key flows](#10-key-flows)
11. [Matching and ranking](#11-matching-and-ranking)
12. [Security and privacy](#12-security-and-privacy)
13. [Environments and deploy](#13-environments-and-deploy)
14. [Testing and CI](#14-testing-and-ci)
15. [Where to go next, and a glossary](#15-where-to-go-next-and-a-glossary)

---

# Part 1: The tour

## 1. The idea in one picture

Causematch is an open-source (MIT), mobile-first web app that works like a dating app for volunteering in Los Angeles County. Volunteers swipe through nonprofit openings ranked for them. When a volunteer likes an opening and the nonprofit says yes too, they are matched.

```mermaid
%%{init: {"flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
flowchart LR
    like["Volunteer<br/>likes an opening"] --> yes["Nonprofit<br/>says yes"]
    yes --> booked["First shift booked,<br/>volunteer told"]
    yes --> chat["It's a match!<br/>Chat opens"]
    booked --> remind["Day before:<br/>Still coming?"]
    remind --> happen["The shift<br/>happens"]
    happen --> marked["Coordinator marks it attended<br/>and confirms the hours"]
    marked --> hours["Hours show on the volunteer's<br/>Shifts and hours page"]
    hours --> cert["Signed certificate<br/>anyone can check by its link"]

    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef planned fill:#999999,stroke:#6b6b6b,color:#ffffff,stroke-dasharray:6 4
    class like,yes,booked,chat,happen step
    class marked,hours,cert key
    class remind planned
```

_Each box is one step a person sees. The darker boxes are the hours record. The dashed box waits for a scheduled job that is not set up yet._

A like creates a match that waits for the nonprofit. The nonprofit's yes books the first shift, tells the volunteer (by email, or by text if they have a phone number on file), and opens a chat. If the volunteer has the deck or Matches open, an "It's a match!" screen appears within about 10 seconds. Once the scheduled job that sends reminders is set up, a message the day before will ask "Still coming?". After the shift, the coordinator marks it attended and confirms how many hours the volunteer gave. Those **confirmed hours** add up, month by month, on the volunteer's own page. For any nonprofit that confirmed some, the volunteer can get a signed **certificate** of those hours, as a page or a PDF, and anyone they give it to (a school or an employer, say) can open its check link to see that it is genuine. Some nonprofits also say they can confirm volunteer hours. A volunteer who needs their hours confirmed (for school, court or a work program) can tick "I need my hours confirmed" and see only those nonprofits. The code calls that setting **verified hours**.

Nonprofits short of people can also post a **last-minute call** (the code and this document also say urgent call): "needs 2 more, tomorrow 9 am". It puts the card at the top of nearby volunteers' decks and can skip the yes step entirely (the first likes are booked at once). Once its scheduled job is set up, it will also text or email the volunteers who opted in.

The pilot covers four causes: food insecurity, environment and parks, animal shelters, and homelessness services (`app/db.py`, `CAUSES`).

**Volunteers**

- Sign in with a link sent to their email address, or with Google or Apple when those are set up. There is no password, and no phone number is needed. A short first form asks for a first name, one box confirming they are 18+ and agree to the privacy policy, a Home place and how they feel about each cause.
- Swipe a ranked deck: Like or Pass. Each card says why it was picked ("♥ Animal shelters", "You're free Sat mornings").
- Keep matches apart from likes still waiting, chat once a nonprofit says yes, add a shift to their calendar, or cancel just one shift ("Can't make this one").
- Keep private preferences on four short pages, and opt in to last-minute calls.
- See upcoming shifts, past shifts and confirmed hours on one **Shifts and hours** page, and get a signed certificate of the hours one nonprofit confirmed.

**Nonprofit coordinators**

- Ask for access to their organization (a claim), then sign in with a link sent by email, or with Google or Apple when that company hosts the mailbox.
- Post openings, edit or reopen them without losing their likes, say yes or no to likes in the Inbox (a No can be undone for two minutes), and chat with matched volunteers.
- Mark shifts attended (confirming the hours) or no-show, add shifts, and post urgent calls.

**Admins**

- Verify nonprofits against IRS records (ProPublica) and approve claims.
- Handle reports and blocks.
- Read the pilot's aggregate metrics. They never see names, phones or emails there.

**Anyone with a check link**

- Open a certificate's check link, or scan the QR code on its PDF, without an account: it says whether the certificate is genuine, withdrawn or unknown, and what it certifies.

## 2. Level 1: system context

Who uses Causematch, and which outside services it talks to.

Levels 1 to 3 use [C4 notation](https://c4model.com): each box gives an element's name, its kind (`[Person]`, `[Software System]`, `[Container]` or `[Component]`, with its technology) and what it does in a few words. Each arrow says what flows, with the protocol in brackets. The shape says the kind too: people are stadiums, external systems are rounded, data stores are cylinders, and a separate process has double sides.

```mermaid
%%{init: {"flowchart": {"curve": "basis", "nodeSpacing": 40, "rankSpacing": 60}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif", "fontSize": "14px"}}}%%
flowchart TB
    vol(["<b>Volunteer</b><br/><small>[Person]</small><br/><br/>Swipes openings, chats,<br/>sees shifts and hours"])
    coord(["<b>Nonprofit coordinator</b><br/><small>[Person]</small><br/><br/>Posts openings, answers likes,<br/>marks shifts"])
    admin(["<b>Admin</b><br/><small>[Person]</small><br/><br/>Verifies nonprofits,<br/>reads pilot metrics"])
    checker(["<b>Certificate checker</b><br/><small>[Person]</small><br/><br/>A school or employer given<br/>a volunteer's certificate"])

    cm["<b>Causematch</b><br/><small>[Software System]</small><br/><br/>Matches volunteers with<br/>nonprofit openings"]

    sm("<b>Secret Manager</b><br/><small>[Software System: Google Cloud]</small><br/><br/>Holds the app's secrets")
    pp("<b>ProPublica</b><br/><small>[Software System]</small><br/><br/>IRS records of nonprofits")
    idp("<b>Google and Apple</b><br/><small>[Software System]</small><br/><br/>Sign-in with an<br/>existing account")
    twilio("<b>Twilio</b><br/><small>[Software System]</small><br/><br/>Optional texts and<br/>number-check codes")
    resend("<b>Resend or Gmail</b><br/><small>[Software System]</small><br/><br/>Sign-in links and notices,<br/>one of the two")
    sched("<b>Cloud Scheduler</b><br/><small>[Software System]</small><br/><br/>Calls the timed jobs")

    vol -- "Swipes, chats<br/><small>[HTTPS]</small>" --> cm
    coord -- "Posts openings, answers likes<br/><small>[HTTPS]</small>" --> cm
    admin -- "Verifies orgs, reads metrics<br/><small>[HTTPS]</small>" --> cm
    checker -- "Opens the check link<br/><small>[HTTPS, no account]</small>" --> cm
    cm -- "Reads secrets at start<br/><small>[env vars]</small>" --> sm
    cm -- "Looks up nonprofits<br/><small>[HTTPS, JSON]</small>" --> pp
    cm -. "Checks sign-ins<br/><small>[HTTPS, OpenID Connect]</small>" .-> idp
    cm -. "Sends texts, codes<br/><small>[HTTPS]</small>" .-> twilio
    cm -. "Sends email<br/><small>[HTTPS (Resend) or SMTP over TLS (Gmail)]</small>" .-> resend
    sched -. "Starts reminders, urgent waves<br/><small>[HTTPS, bearer token]</small>" .-> cm

    classDef person fill:#08427b,stroke:#052e56,color:#ffffff
    classDef system fill:#1168bd,stroke:#0b4884,color:#ffffff
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    classDef planned fill:#999999,stroke:#6b6b6b,color:#ffffff,stroke-dasharray:6 4
    class vol,coord,admin,checker person
    class cm system
    class sm,pp external
    class idp,twilio,resend,sched planned
```

_Legend: dark-blue stadiums = people, blue box = Causematch, grey rounded boxes = external systems. A dashed grey box or arrow is built or planned but not live yet._

What is honest to say about each outside service today:

- **Google Cloud is live.** Causematch runs on Cloud Run in `us-west1` (the preview), and reads its secrets from Secret Manager. Hosting is where it runs, not something it talks to, so it is not an arrow here; section 13 has the deploy picture.
- **ProPublica is live.** The claim page's name search, the admin pages and two scripts read it.
- **Google and Apple sign-in is built but not live.** `app/oauth.py` has real clients for both, each offered only when it is configured. The preview runs a fake for Google alone (`CAUSEMATCH_FAKE_OAUTH=1`): a local consent page, where each browser signs a volunteer in under its own random subject with whatever email is typed there. Only staff sign-ins are limited to the demo's coordinators.
- **Email is built but not live, and the emailed link is the one way in that is always there.** `app/notify.py` has real senders for Resend and for a Gmail account over SMTP (the beta's stopgap until there is a domain to send from; Resend wins when both are set). The preview runs a fake instead: nothing is sent, and a sign-in link is shown on the page for the demo's addresses. Production refuses to start without one of the two.
- **Twilio is optional and not live.** It only sends texts and the code that proves a number a volunteer adds under Me, which is offered only with `CAUSEMATCH_COLLECT_PHONE=1`. A phone number is never a way to sign in, so without Twilio nothing breaks: volunteers get email. The preview runs a fake (the code is always `000000`, and only fictional 555-01xx numbers get one).
- **Cloud Scheduler is not set up.** The two endpoints it would call exist, but nothing calls them yet, so the preview sends no day-before reminders and no urgent waves on a timer.

The certificate checker is the one person who never needs an account. They reach Causematch only through a check link a volunteer gave them, and see only what that certificate says (section 12). In the preview the password gate stands in front of that link too, so only someone with the preview password can open it.

The public website (`site/`, published to GitHub Pages by `.github/workflows/pages.yml`) is a static page with the demo video. It is not part of the running app.

## 3. Level 2: containers

In C4, a "container" is something that runs or holds data. Causematch has very few.

```mermaid
%%{init: {"flowchart": {"curve": "basis", "nodeSpacing": 40, "rankSpacing": 60}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif", "fontSize": "14px"}}}%%
flowchart TB
    vol(["<b>Volunteer</b><br/><small>[Person]</small><br/><br/>Swipes openings, chats,<br/>sees shifts and hours"])
    staff(["<b>Coordinator or admin</b><br/><small>[Person]</small><br/><br/>Runs openings, or<br/>verifies nonprofits"])
    checker(["<b>Certificate checker</b><br/><small>[Person]</small><br/><br/>A school or employer given<br/>a volunteer's certificate"])

    subgraph cm["Causematch [Software System]"]
        direction TB
        ui["<b>Web pages</b><br/><small>[Container: HTML, htmx, small JS]</small><br/><br/>Server-rendered pages<br/>in the browser"]
        app["<b>Web application</b><br/><small>[Container: Python 3.12, FastHTML, uvicorn]</small><br/><br/>Pages, sign-in, ranking, matches,<br/>chat, certificates, task endpoints"]
        worker[["<b>Photo worker</b><br/><small>[Container: Python child process]</small><br/><br/>Decodes untrusted images<br/>under tight limits"]]
        db[("<b>Database</b><br/><small>[Container: SQLite file]</small><br/><br/>Every table, photos included")]
    end

    pp("<b>ProPublica</b><br/><small>[Software System]</small><br/><br/>IRS records of nonprofits")
    idp("<b>Google and Apple</b><br/><small>[Software System]</small><br/><br/>Sign-in with an<br/>existing account")
    twilio("<b>Twilio</b><br/><small>[Software System]</small><br/><br/>Optional texts and<br/>number-check codes")
    resend("<b>Resend or Gmail</b><br/><small>[Software System]</small><br/><br/>Sign-in links and notices,<br/>one of the two")
    sched("<b>Cloud Scheduler</b><br/><small>[Software System]</small><br/><br/>Calls the timed jobs")

    vol -- "Uses<br/><small>[HTTPS]</small>" --> ui
    staff -- "Uses<br/><small>[HTTPS]</small>" --> ui
    checker -- "Opens the check link<br/><small>[HTTPS, no account]</small>" --> ui
    ui -- "Forms, htmx swaps, chat<br/><small>[HTTPS, WebSocket]</small>" --> app
    ui -. "Consent page<br/><small>[HTTPS redirect]</small>" .-> idp
    app -- "Sends an upload,<br/>gets a clean JPEG<br/><small>[stdin/stdout]</small>" --> worker
    app -- "Reads and writes<br/><small>[apsw, one connection]</small>" --> db
    app -- "Looks up nonprofits<br/><small>[HTTPS, JSON]</small>" --> pp
    app -. "Checks ID tokens<br/><small>[HTTPS, OIDC]</small>" .-> idp
    app -. "Sends texts, codes<br/><small>[HTTPS]</small>" .-> twilio
    app -. "Sends email<br/><small>[HTTPS or SMTP over TLS]</small>" .-> resend
    sched -. "Reminders, urgent waves<br/><small>[HTTPS, bearer token]</small>" .-> app

    classDef person fill:#08427b,stroke:#052e56,color:#ffffff
    classDef container fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    classDef planned fill:#999999,stroke:#6b6b6b,color:#ffffff,stroke-dasharray:6 4
    class vol,staff,checker person
    class ui,app,worker,db container
    class pp external
    class idp,twilio,resend,sched planned
    style cm fill:none,stroke:#888888,stroke-dasharray:8 4,color:#888888
```

_Legend: blue = containers inside the dashed Causematch boundary (cylinder = data store, double sides = a separate process); dark-blue stadiums = people; grey rounded boxes = external systems, dashed when not live yet._

**The browser.** Pages are plain HTML built on the server, so forms work without JavaScript. The pages still count as a container of their own because a good deal runs in the browser: htmx swaps, the websocket chat, the 10-second poll for a new match and a few small scripts. [htmx](https://htmx.org) is a small library that lets a button fetch a piece of HTML and swap it into the page. Causematch uses it for swiping, for the chat, and to ask every 10 seconds whether a nonprofit said yes. The chat uses a websocket, a connection that stays open so the server can push new messages. A few small scripts in `static/` add touches such as dragging a card (`swipe.js`), using the device location (`geo.js`) and shrinking a photo before upload (`photo.js`). A web app manifest and icons (static files, not a container) let people add Causematch to their Home Screen like an app (a PWA, "progressive web app"). For Google or Apple sign-in, the browser visits that company's consent page and comes back to `/auth/*/callback`. For the emailed link, the browser opens the link from the mail, lands on a confirm page and taps its button.

**The web application.** One image (`Dockerfile`) runs uvicorn, a Python web server, which runs the app in `main.py`. The app is built with [FastHTML](https://fastht.ml), a Python framework in which each page is a Python function that returns HTML. The same app serves the two task endpoints that Cloud Scheduler would call (`/tasks/reminders` hourly, `/tasks/urgent` every 10 minutes); they are routes, not a separate container. The notices a request raises (a new like, an accept, an automatic accept, a withdrawal, a cancelled shift) and the sign-in links are sent after the response is out, in four worker threads of their own, so a slow mail server never holds a page. A new chat message's notice goes the same way: after `POST /chat/{id}/send`'s redirect, or, from a websocket frame, in a task beside the socket, so the sender's next frame never waits on it. Claim approval and rejection emails are the exception: they are sent during the admin's request, since the page says whether they went. Its secrets come from Google Cloud's Secret Manager as environment variables at start, which the picture leaves out. Photo uploads are decoded in a separate child process, `app/photo_worker.py`, with tight CPU and memory limits, so a hostile image can't harm the app. Certificate PDFs, by contrast, are built inside the app process, in a thread, from data the app wrote itself.

**The SQLite file.** All data lives in one SQLite file, photos included, read and written through one shared connection. At every start, numbered migrations run first and are the only thing that changes an existing database's schema; then `app/db.py` creates any table that doesn't exist yet (its idempotent start-up statements aside, see section 9) and stops the app if an existing table lacks a column its class declares.

**Why one process and one instance.** The SQLite file sits on the container's own disk, and the persistent rate limits and email ceilings are counted in it. Open chat sockets and the token buckets live in that process's memory. (A token bucket is a per-user counter for rate limits: it allows a short burst, then refills at a steady rate.) A second instance would have its own copy of all of it, so two people could see different data, and each would count its own emails against a Gmail account's daily limit, which belongs to the account. The preview deploy therefore caps the service at one instance (`--max-instances 1`), and production would need the same cap, or shared state. Cloud Run can scale to zero when idle, so there is no background loop: timed work (reminders, urgent waves) waits for Cloud Scheduler to call an endpoint. The preview keeps one instance warm by default, so a demo never waits for a cold start, but its database still lives on the instance's disk and is wiped whenever the instance restarts or is redeployed, as the preview's banner says. A persistent database for production (Litestream copying it to Cloud Storage) is planned, not built.

## 4. Level 3: components inside the app

Every request passes the same layers, top to bottom.

```mermaid
%%{init: {"flowchart": {"curve": "basis", "nodeSpacing": 40, "rankSpacing": 90}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif", "fontSize": "14px"}}}%%
flowchart TB
    ui["<b>Web pages</b><br/><small>[Container: HTML, htmx, small JS]</small><br/><br/>Server-rendered pages<br/>in the browser"]

    subgraph app["Web application [Container: Python 3.12, FastHTML, uvicorn]"]
        direction TB
        mw["<b>Middleware</b><br/><small>[Component: security.py, notify.py, Starlette sessions]</small><br/><br/>Guard, PreviewGate, RunAfter,<br/>session cookie"]
        bw["<b>Beforeware</b><br/><small>[Component: Python modules, auth.py, security.py]</small><br/><br/>Who you are, your role,<br/>rate limits"]
        rv["<b>Volunteer routes</b><br/><small>[Component: Python modules, app/routes/*.py]</small><br/><br/>Deck, matches, chat, preferences,<br/>shifts, hours, certificates"]
        rs["<b>Staff and public routes</b><br/><small>[Component: Python modules, app/routes/*.py]</small><br/><br/>Nonprofit, admin, sign-in,<br/>claims, tasks, check link"]
        data["<b>Domain data</b><br/><small>[Component: Python module, data.py]</small><br/><br/>Every query, under one lock"]
        rank["<b>Ranking</b><br/><small>[Component: Python modules, match.py, prefs.py]</small><br/><br/>Scores openings from<br/>preferences"]
        notify["<b>Notices</b><br/><small>[Component: Python module, notify.py]</small><br/><br/>Email and texts,<br/>once each"]
        photos["<b>Photos</b><br/><small>[Component: Python module, photos.py]</small><br/><br/>Checks and re-encodes images"]
        oauth["<b>Provider sign-in</b><br/><small>[Component: Python module, oauth.py]</small><br/><br/>Google and Apple flows,<br/>token checks"]
        metrics["<b>Metrics</b><br/><small>[Component: Python module, metrics.py]</small><br/><br/>Aggregate pilot numbers"]
        cert["<b>Certificates</b><br/><small>[Component: Python modules, signing.py, certificate.py]</small><br/><br/>Signs hours snapshots,<br/>builds the PDF and QR code"]
    end

    worker[["<b>Photo worker</b><br/><small>[Container: Python child process]</small><br/><br/>Decodes untrusted images"]]
    db[("<b>Database</b><br/><small>[Container: SQLite file]</small><br/><br/>Every table, photos included")]
    msg("<b>Resend or Gmail, and Twilio</b><br/><small>[Software System]</small><br/><br/>Email, and optional texts")
    idp("<b>Google and Apple</b><br/><small>[Software System]</small><br/><br/>Account sign-in")
    pp("<b>ProPublica</b><br/><small>[Software System]</small><br/><br/>IRS records of nonprofits")
    sched("<b>Cloud Scheduler</b><br/><small>[Software System]</small><br/><br/>Calls the timed jobs")

    ui -- "Sends requests<br/><small>[HTTPS, WebSocket]</small>" --> mw
    sched -. "Calls /tasks/*<br/><small>[HTTPS, bearer token]</small>" .-> mw
    mw -- "Passes checked requests<br/><small>[ASGI]</small>" --> bw
    bw -- "Hands to<br/><small>[Python call]</small>" --> rv
    bw -- "Hands to<br/><small>[Python call]</small>" --> rs
    rv -- "Reads and writes<br/><small>[Python call]</small>" --> data
    rs -- "Reads and writes<br/><small>[Python call]</small>" --> data
    rv -- "Notifies<br/><small>[Python call]</small>" --> notify
    rs -- "Notifies<br/><small>[Python call]</small>" --> notify
    rv -- "Re-encodes<br/><small>[Python call]</small>" --> photos
    rs -- "Re-encodes logos<br/><small>[Python call]</small>" --> photos
    rv -- "Connects accounts<br/><small>[Python call]</small>" --> oauth
    rs -- "Signs people in<br/><small>[Python call]</small>" --> oauth
    rs -- "Builds the numbers<br/><small>[Python call]</small>" --> metrics
    rv -- "Builds the PDF<br/><small>[Python call]</small>" --> cert
    rs -- "Looks up nonprofits<br/><small>[HTTPS, JSON]</small>" --> pp
    data -- "Scores the deck<br/><small>[Python call]</small>" --> rank
    data -- "Signs and verifies<br/><small>[Python call]</small>" --> cert
    notify -- "Claims each notice<br/><small>[Python call]</small>" --> data
    photos -- "Decodes in<br/><small>[stdin/stdout]</small>" --> worker
    data -- "Reads and writes<br/><small>[apsw, one connection]</small>" --> db
    notify -. "Sends<br/><small>[HTTPS, SMTP over TLS]</small>" .-> msg
    oauth -. "Checks ID tokens<br/><small>[HTTPS, OIDC]</small>" .-> idp

    classDef container fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef component fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef external fill:#999999,stroke:#6b6b6b,color:#ffffff
    classDef planned fill:#999999,stroke:#6b6b6b,color:#ffffff,stroke-dasharray:6 4
    class ui,worker,db container
    class mw,bw,rv,rs,data,rank,notify,photos,oauth,metrics,cert component
    class pp external
    class msg,idp,sched planned
    style app fill:none,stroke:#888888,stroke-dasharray:8 4,color:#888888
```

_Legend: light blue = components inside the Web application container (dashed boundary); mid blue = the neighbouring containers; grey rounded boxes = external systems, dashed when not live yet. The email provider (Resend or Gmail) and Twilio are separate systems drawn as one box here, to keep this diagram legible; Level 2 draws them apart._

**Middleware** is code that wraps every request before any page code runs. `security.Guard` refuses cross-site form posts and oversized bodies, and adds security headers. `security.PreviewGate` asks for the preview password (only in the preview). `notify.RunAfter` sits just inside them: once a response is out, it runs the notices that handlers queued with `notify.after`. Then a signed session cookie says who is signed in.

**Beforeware** is FastHTML's name for checks that run before a page handler. `auth.check_session` makes sure the session is still valid and that the role fits the path: `/admin/*` is for admins, `/org/*` for coordinators, and the other signed-in pages for volunteers. `security.throttle` takes a token from the user's bucket for swipes, chat, reports and uploads.

**Routes** live in `app/routes/`, one file per area. They build HTML and never touch the database directly. The public check link, `/check/{id}`, lives in `app/routes/hours.py` next to the volunteer's certificate pages; the picture counts it with the public routes.

**The domain layer** is where the rules live. `app/data.py` holds every query the routes run, and every public function in it runs under one lock, because the single database connection is shared by all threads. `app/match.py` is pure scoring with no database access, so it is easy to test. `app/notify.py` sends emails (Resend, or a Gmail account over SMTP under daily ceilings) and, for volunteers who added a number, texts. `app/photos.py` checks and re-encodes images. `app/oauth.py` talks to Google and Apple and checks what they send back. `app/metrics.py` builds the admins' aggregate numbers from rows that `app/metrics_rows.py` reads. `app/signing.py` signs and checks hours certificates with a secret key, and `app/certificate.py` lays one out as a PDF with a QR code; both are pure functions with no database access. `app/propublica.py` is the ProPublica client that the claim page and the admin pages use (with `app/org_import.py` for imports); the picture folds it into the staff routes. Routes also read preferences directly through `app/prefs.py`.

**Storage.** `app/db.py` declares each table as a Python class. A migration is a numbered script in `app/migrations/` that usually changes data or structure that a class change alone can't. Pending migrations run at every start, and for new changes they alone change an existing table: the classes only create tables that are missing, and the app stops if a table lacks a column its class declares.

**Life of one request: a swipe.**

1. The volunteer taps Like or drags the card right. htmx sends `POST /swipe` with the opening's id.
2. `Guard` checks the request comes from the app's own origin. `PreviewGate` checks the access cookie (preview only). The session cookie gives the user id.
3. `auth.check_session` loads the profile, checks it is a volunteer who finished onboarding, and `security.throttle` takes one token from the swipe bucket (30, then 1 a second).
4. `app/routes/matches.py` moves the work to a thread and calls `data.swipe_created`.
5. Under the lock, `app/data.py` runs the same visibility check the deck uses, so a like sent by hand for a card the volunteer couldn't see is refused. It records the swipe and creates a `pending` match.
6. The handler returns only the HTML of the next card. htmx swaps it into the page and refreshes the number on the Matches tab. (After a volunteer's very first like, it sends them to two optional questions instead.)
7. Once that response is out, `RunAfter` runs what the handler queued with `notify.after`: `notify.on_new_like` emails the org's coordinators, at most once per org every 15 minutes. Only 20 likes in any 24 hours from one volunteer send this email; later ones just show in the inbox (`security.LIKE_NOTICES`).

## 5. Key ideas

These pictures are not C4 diagrams; they show how one idea works. They use the same colours: light blue for steps, mid blue for the result that matters, grey and dashed for what isn't live yet, and dark-blue stadiums for people.

### 5.1 A match is two likes

```mermaid
%%{init: {"themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
stateDiagram-v2
    direction LR
    [*] --> pending: volunteer likes
    [*] --> accepted: like on an auto-accept urgent call
    pending --> accepted: nonprofit says yes
    pending --> declined: nonprofit says no
    declined --> pending: coordinator undoes within 2 minutes
    pending --> withdrawn: volunteer withdraws
    accepted --> withdrawn: volunteer withdraws, or a block

    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    class pending,declined,withdrawn step
    class accepted key
```

A like is the volunteer's half. It creates a `match` row in `pending`, listed under "Your likes" on the Matches page. The coordinator's yes is the other half: it moves the match to `accepted`, but only while the opening still has a free slot, in one database statement, so two quick yeses can't overbook. Accepting books the first shift, tells the volunteer and opens the chat. A coordinator's No can be taken back for two minutes (`data.UNDO_DECLINE`); until then the volunteer is shown nothing and the like still reads as waiting. Withdrawing, a block either way, or the org's deactivation ends a match and cancels its future shifts. The full list of transitions is in [section 10](#10-key-flows).

### 5.2 How the deck is chosen

```mermaid
%%{init: {"flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
flowchart TB
    all["Every open opening<br/>that starts in the future"]
    hard["Always hidden<br/>unverified org, already swiped,<br/>a cause marked Not for me,<br/>past how far ahead I look"]
    only["My Only show choices<br/>distance, causes, when, kind of work,<br/>and I need my hours confirmed"]
    sheet["This visit's filter sheet<br/>travel time, hidden causes, this week"]
    rank["Rank<br/>urgent calls first, a full week last,<br/>then the fit score"]
    cards["Cards, one at a time<br/>with 1 to 4 reasons why"]
    all --> hard --> only --> sheet --> rank --> cards

    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    class all,hard,only,sheet,rank step
    class cards key
```

_Each step narrows the list; only the last one orders it._

The deck is a funnel, then a sort. Some rules always hide an opening, including one that starts beyond the volunteer's horizon: how far ahead they chose to look (this week, 2 weeks, a month or any time). Others only hide it when the volunteer chose "Only show" for that preference; otherwise the same preference just ranks the opening lower. Distance hides by default. The filter sheet changes the deck for this visit only and is never saved. The fit score (`app/match.py`) is a weighted sum of how well the causes, distance, times, skills, kind of work and languages fit, plus how quickly the org usually answers. When the deck is empty, the app offers the one change that would add the most cards. Weights and details are in [section 11](#11-matching-and-ranking).

### 5.3 Notifications are sent exactly once

```mermaid
%%{init: {"flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
flowchart LR
    event["Something happened<br/>a yes, a message, a shift tomorrow"]
    claim{"Claim the key<br/>in the notification table"}
    ceiling{"Under the<br/>SMS ceiling?"}
    send["Send the text<br/>or the email"]
    stop["Stop:<br/>already sent"]
    later["Recorded deferred,<br/>tried again later"]
    result["Recorded sent, failed<br/>or unknown"]
    event --> claim
    claim -- "already claimed" --> stop
    claim -- "won" --> ceiling
    ceiling -- "no" --> later
    ceiling -- "yes, or an email" --> send --> result

    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef off fill:#999999,stroke:#6b6b6b,color:#ffffff
    class event,claim,ceiling,later step
    class send,result key
    class stop off
```

_The key, such as `reminder:{shift}`, is what makes a send happen once._

Every notice has a key that names the event: `accepted:{match}`, `reminder:{shift}`, `urgent:{call}:{volunteer}`. The `notification` table holds one row per key, with a unique index on it. Before sending, `app/notify.py` claims the key in one conditional database statement, so if a request is retried or two runs overlap, only one of them wins and sends. A volunteer gets a text when they added a number and Twilio is set up, otherwise an email (most volunteers sign in by email and never add a number, so most notices are emails); both use the same key, so nothing repeats. Request handlers queue notices and sign-in links to send after the response is out (`notify.after`), so a slow mail server never holds a page. The chat queues its new-message notice the same way once it has saved and broadcast the message; from a websocket frame, the notice starts at once in a task of the socket's own, so the socket's next frame doesn't wait on it. Only claim decisions are not queued: they are emailed inside the admin's request, whose page says whether they went. A failed send can be claimed again, up to 5 tries. A timeout is recorded `unknown` and is not retried, because the text may have arrived: a lost notice is better than a double text. A global ceiling caps texts per hour and per day, and a text over it is recorded `deferred`. Email sent over Gmail has ceilings of its own (section 12): an email they refuse is recorded `failed`, so the retry rules above apply, not `deferred`. The hourly sweep resends deferred accept and message texts; a deferred reminder goes again only if a later sweep still finds its shift 23 to 25 hours ahead; a deferred urgent text may be retried by the call's second wave.

### 5.4 Urgent calls go out in waves

```mermaid
%%{init: {"flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
flowchart LR
    coord(["Coordinator"])
    w1["Wave 1<br/>best-fit volunteers who opted in,<br/>up to 3 x N, at most 25"]
    w2["Wave 2, 30 min later<br/>if still short"]
    vol(["Volunteer"])
    booked["Auto-accept on:<br/>booked at once"]
    pend["Auto-accept off:<br/>waits for the nonprofit"]
    coord -- "posts a call:<br/>needs N, within 48 h" --> w1 --> w2
    w1 & w2 -- "text or email<br/>with a link" --> vol
    vol -- "likes the card<br/>on top of the deck" --> booked
    vol -- "likes" --> pend

    classDef person fill:#08427b,stroke:#052e56,color:#ffffff
    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef planned fill:#999999,stroke:#6b6b6b,color:#ffffff,stroke-dasharray:6 4
    class coord,vol person
    class pend step
    class booked key
    class w1,w2 planned
```

_The waves are dashed: they go out when Cloud Scheduler calls `/tasks/urgent`, planned every 10 minutes and not set up yet. The card ranking first works today._

An urgent call is "need N more people for this shift in the next 48 hours". Its card ranks first for every volunteer whose deck already holds that opening. Once the waves run, volunteers who opted in to last-minute calls will also get a text, or an email if they have no usable number. Notices go out in at most two waves, so a call never texts everyone at once. Each wave picks the best-fit volunteers who are free then, within travel time, outside their quiet hours, under their weekly cap of notices, and not already booked close to that time. An org may post 3 calls in any 24 hours. When a volunteer cancels a shift less than 48 hours ahead and gives up their spot, the app posts a call for 1 person by itself (a "refill").

### 5.5 Shifts and hours

```mermaid
%%{init: {"themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
stateDiagram-v2
    direction LR
    [*] --> scheduled: accept, or the coordinator adds one
    scheduled --> cancelled: either side cancels before it starts
    scheduled --> completed: Attended, hours confirmed
    scheduled --> no_show: No-show
    completed --> no_show: one correction
    no_show --> completed: one correction

    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    class scheduled,cancelled,no_show step
    class completed key
```

A **shift** is one dated occurrence of volunteering on an accepted match, with a length (1 to 8 hours), so its time reads as a range ("9:00 AM to 12:00 PM").

- **Before it starts**, the volunteer can answer "Still coming?" from the reminder's link, or cancel just that shift ("Can't make this one").
- **After it starts**, the coordinator taps Attended, which confirms the hours (pre-set to the shift's length), or No-show.
- **Confirmed hours** add up by month on the volunteer's **Shifts and hours** page.
- **A certificate** lists the hours one nonprofit confirmed, for one month or for all of them, with the volunteer's full name. It is frozen when issued and signed with a secret key, so it never changes with the profile and an edit to it shows. The volunteer can view it, print it, download it as a PDF with a QR code, or withdraw it. Its **check link** tells anyone who opens it whether it is genuine, whether the nonprofit has corrected those hours since, or that it was withdrawn. Hours confirmed before the volunteer last changed their name can't go on one, so one person's hours never appear under two names.
- **Verified hours** is a private checkbox, "I need my hours confirmed": ticked, the deck shows only the nonprofits that say they can confirm hours. Their cards say "Confirms your hours" to every volunteer.

The exact rules are in [section 10](#10-key-flows), flows (g), (h) and (i).

### 5.6 Privacy: who sees what

```mermaid
%%{init: {"flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
flowchart LR
    org(["Nonprofit"])
    before["Before a like<br/>sees nothing"]
    liked["After a like<br/>Sam L., photo, bio, causes,<br/>general times, languages"]
    yes["After the yes<br/>Sam Lee, the chat"]
    never["Never shared<br/>phone, email, places, sign-in accounts,<br/>cause ratings, Only show choices,<br/>weekly limit, verified-hours choice"]
    org --> before --> liked --> yes

    classDef person fill:#08427b,stroke:#052e56,color:#ffffff
    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    classDef off fill:#999999,stroke:#6b6b6b,color:#ffffff
    class org person
    class before,liked step
    class yes key
    class never off
```

_What a nonprofit sees grows with each step. The grey box never leaves the volunteer's account._

A nonprofit learns about a volunteer only when that volunteer likes one of its openings. Then it sees what the volunteer agreed to share, with the name cut to first name and last initial ("Sam L."). The full last name appears once the nonprofit says yes. Places are stored as an area, never an address, and cards show the travel minutes from the nearest place, never coordinates. Photos are re-encoded on the server, which strips location data. Blocks work both ways and hide a volunteer's details from that org at once. A certificate is the one thing the volunteer hands out themselves: its check link shows their full name, the nonprofit and its EIN, the period, the total, the issue date and the signature code to whoever holds the link, and whether the hours have changed since, and nothing more (section 12 lists it). The full table, with file paths, is in [section 12](#12-security-and-privacy).

---


# Part 2: Reference

## 6. Features by role

Volunteers see a bottom tab bar with **Openings** (`/deck`), **Matches** (`/matches`) and **Me** (`/profile`), defined in `app/ui.py` (`TABS`). The Matches tab carries a small number: how many of the volunteer's likes a nonprofit accepted or declined since they last opened Matches, "9+" past nine (`app/ui.py`, `MatchesDot`; `data.match_news_count`). Their own likes still waiting don't count. Coordinators see a top nav with **Openings** (`/org`), **Inbox**, **Shifts** and **Organization** (`ORG_TABS`). The Inbox tab counts the likes waiting for an answer plus the chats owed a reply, and the Shifts tab the shifts that started and wait to be marked (`data.org_badges`). Admins see **Admin**, **Metrics**, **Reports**, **Orgs**, **Claims** and **Blocks** (`app/routes/admin.py`, `ADMIN_TABS`).

### Volunteer

| Feature | What it does | Route(s) | Code |
|---|---|---|---|
| Sign in by emailed link | Enter an email address on `/login` and get a single-use link that expires in 15 minutes. The first sign-in creates the account, with no phone and no name until onboarding, so there is no separate sign-up; if the address already belongs to a volunteer's Google or Apple account, it opens that account instead (10a). The page answers the same "Check your email" for every address, with a "Send it again" button and a way back to the form. What is sent depends on the address, and the mail goes out after the response: a sign-in link to the volunteer who holds the address (signed up with it, or has it through Google on Gmail or Apple on iCloud), a sign-up link to an address no volunteer holds, the staff link (the same as `/org/login` sends) to a coordinator's or admin's address, and nothing to a mailbox that a suspended account holds or, for a new account, to a staff mailbox (a `+tag` or Gmail dot variant of a staff address, or a staff address that can't sign in). The link opens a confirm page, "Sign in as {address}", that signs in only when its button is pressed (see 10a). In a demo (`CAUSEMATCH_DEMO_MATCHES=1`) a "Try it as a volunteer" button signs in a fresh demo volunteer in one tap; elsewhere that route answers 403. | `GET /login`, `POST /login/send`, `GET/POST /login/verify`, `POST /login/demo`, `POST /logout` | `app/routes/auth.py`, `app/auth.py`, `app/notify.py`, `app/data.py` (`volunteer_link_kind`, `create_email_token`, `consume_email_token`) |
| Sign in with Google or Apple | "Continue with Apple" and "Continue with Google" on `/login`, each shown only when that provider is configured (a preview shows Google alone, through the fake). The provider's verified email is required. The first sign-in creates a volunteer with no phone, that email and the given name, unless the provider's verified address is the exact address a volunteer already holds, whose account it then opens (10a). Only a holder can be joined: an emailed-link account, or a Google or Apple one on a mailbox the provider runs itself (Gmail, iCloud), and never when that volunteer already has an account of this provider. A coordinator's or admin's mailbox signs no new volunteer up and joins none here: the answer is the emailed link's for that address (the mailbox is compared by `forms.abuse_key`, so Gmail dot variants count, and so does a staff address that can't sign in). Onboarding offers the Google profile picture behind an unticked box. See 10c. | `GET /auth/{google,apple}/start`, `GET /auth/google/callback`, `POST /auth/apple/callback` | `app/routes/oauth.py`, `app/oauth.py` |
| Sign-in methods and reach | Under Me, Your profile: "Sign-in methods" shows the address of the emailed link (or "none") and each provider connected ("Connected as" its email, Disconnect) or offered (Connect), never removing the last way in that works. Connect is refused ("already connected to another Causematch account") when another live volunteer holds the account's address, and refused for a staff mailbox. Below it, how notices arrive: by email to the address on file, or by text to a number the volunteer added. The add-a-number form, which proves the number with a texted code, shows only with `CAUSEMATCH_COLLECT_PHONE=1` (off by default: the beta asks for no phone number) and while Twilio Verify is configured, and a number is never a way to sign in. A disconnect signs out every other session. | `POST /me/identity/{google,apple}/{connect,disconnect}`, `POST /me/phone/send`, `POST /me/phone/verify` | `app/routes/profile.py`, `app/routes/oauth.py` |
| Onboarding | A short form: first name, a Home place (neighborhood, ZIP or device location) with travel mode and time (car, 30 minutes to start), causes rated love / open / not for me (each starts at open), and one consent box, "I'm 18 or older and I agree to the privacy policy", which sits with the button in a bar that stays in reach. After a Google or Apple sign-in the first name comes prefilled and the email is kept; Google's profile picture is offered behind an unticked "Use my Google photo" box. Volunteers can't reach the rest of the app until it is done. After the first like, one optional card asks whether they volunteered in the last 12 months and how they heard of us; it is shown once. Free times, email and the rest are set later under Me. | `GET/POST /onboarding`, `GET/POST /onboarding/more` | `app/routes/onboarding.py` |
| Preferences | Kept private, unless a page says what a nonprofit sees. An index with four pages, each its own form that posts only its own fields, so saving one leaves the others as they were. **Causes and places:** the three-state cause ratings, "I need my hours confirmed" (one checkbox: ticked, only nonprofits that confirm hours are shown), "Surprise me", and up to 2 places, each with mode and max travel time. A saved second place stays folded behind a one-line summary ("Venice · Walk, up to 15 min"), and opens only after a failed save. **When:** a 7×3 day grid, up to 10 specific free dates, how far ahead to look, one-time or recurring, and "Up to this many shifts a week" (off, 1 to 5). **Kind of work:** setting, effort, group size, first-timer friendly, whether a separate orientation is OK, spoken languages, how often, and skills (`choices.SKILLS`, music, art and performing among them). **Last-minute calls:** see below. | `GET /me/preferences`, `GET /me/preferences/causes`, `.../when`, `.../work`, `.../urgent`, `POST /me/preferences` | `app/routes/prefs.py`, `app/prefs.py` |
| Dealbreakers ("Only show") | For each of causes, distance, when and kind of work, the volunteer picks whether it only ranks openings or also hides those that don't fit. Each toggle sits on the page of the preference it governs. Distance hides by default. "Not for me" causes always hide. | part of the preference pages | `app/choices.py` (`DEALBREAKERS`), `app/data.py` |
| Swipe deck | One card at a time. Each card shows a photo, title, org, causes, the time as a range when the opening gives a shift length, the travel minutes from the nearest place with the neighborhood ("7 min by car · Palms"), or the neighborhood alone when the volunteer has no place, or "Remote" for a remote opening, and 1 to 4 "why" chips. A card within 4 hours of a shift the volunteer already has says "You're booked at {org} then"; a card in a week that is already full says "Your week is full". Like or Pass by button or by dragging sideways; the number of openings shows under the buttons. Swipes are sent by htmx. A nonprofit's yes arrives as an "It's a match!" takeover (see the next row). | `GET /`, `GET /deck`, `POST /swipe` | `app/routes/deck.py`, `app/routes/matches.py`, `app/data.py`, `app/match.py`, `static/swipe.js` |
| "It's a match!" | A full-screen takeover: the nonprofit's face and the volunteer's picture with a heart, the nonprofit and the opening, then Say hi and Keep swiping (Close on Matches). Several new matches at once are listed under it. It arrives live: the deck and Matches pages ask `GET /deck/news` every 10 seconds and when the tab comes back, and a swipe's answer carries it too. The poll's answer lands in a slot outside the deck, so a later swipe doesn't reset it. With nothing new, the poll only refreshes the Matches number. Claiming the new matches is one conditional update, so two requests never both draw it. | `GET /deck/news` | `app/routes/deck.py` (`MatchSheet`, `MomentSlot`), `app/data.py` (`new_matches`), `static/sheet.js` |
| "Next 48h" | A switch that limits the deck to openings starting in the next 48 hours, sorted soonest then nearest. | `GET /deck?today=1` | `app/routes/deck.py` |
| Filter sheet | Temporary filters for this visit, stored in the session and never in saved preferences: the main place's travel time, causes to hide, and this week only. | `POST /deck/filters` | `app/routes/deck.py` |
| Empty-deck help | When nothing is left, the app offers the one change that would add the most cards (widen a travel time, look further ahead, drop a dealbreaker, show nonprofits that don't confirm hours, or clear filters). | `POST /deck/relax` | `app/routes/deck.py`, `app/data.py` |
| Matches | Three parts: "Matches" (the nonprofits that said yes), "Your likes" (likes still waiting, with a line saying a like becomes a match when the nonprofit says yes), and a closed "Past" fold for declined and withdrawn ones, which opens and counts them when a decline is new. Status labels: Waiting for the nonprofit, Matched, Not this time, Withdrawn (`app/ui.py`, `MATCH_STATUS`; the nonprofit's inbox calls the same state Accepted). During a decline's undo hold the card still reads Waiting and shows no Withdraw link. An accepted card leads with a Chat button, right under the title and the shift's time. Under "More" sit Add to calendar (an `.ics` file and a Google Calendar link), "Can't make this one", the nonprofit's event link, Withdraw, Report and Block. Withdrawing a pending or accepted match cancels its future shifts; withdrawing an accepted one emails the org. A link at the top opens Shifts and hours. | `GET /matches`, `GET/POST /matches/{id}/withdraw`, `GET /shifts/{id}.ics` | `app/routes/matches.py`, `app/routes/shifts.py`, `app/ui.py` |
| Chat | Opens once the nonprofit accepts. The header names the shift: the next one's date, time and neighborhood, else the opening's time. When the nonprofit edits the opening, a quiet centered line says which fields changed. Messages arrive live over a websocket, and a plain POST form works as a fallback. The chat becomes read-only after a withdrawal, a block either way, or the org's deactivation. | `GET /chat/{id}`, `POST /chat/{id}/send`, websocket `/chat/{id}/ws` | `app/routes/chat.py`, `static/chat.js` |
| Shifts and hours | The first shift is booked automatically on accept when the opening's (or urgent call's) start is still ahead; otherwise the coordinator adds one. One page, titled "Shifts and hours", lists upcoming and past shifts, then the hours: confirmed hours by month, each shift with its nonprofit and the coordinator who confirmed it, and apart from those any attended shifts that have no confirmed hours. A Certificates part, above the hours by month, offers one card per nonprofit that confirmed some, with the latest month preselected (see the next row); each attended shift that has confirmed hours links to its nonprofit's card with "Get a certificate". `/hours` redirects there. | `GET /history`, `GET /hours` | `app/routes/shifts.py`, `app/routes/hours.py`, `app/data.py` (`my_shifts`, `my_hours`) |
| Hours certificates | Per nonprofit, the volunteer picks a month or "All my hours here" and taps "Get a certificate". The certificate page shows their full name, the nonprofit with its EIN and website, each shift (date, role, hours), the total, the coordinators who confirmed them, the issue date, a signature code and the check link; it prints without the tab bar. "Download PDF" gives the same on US Letter with a QR code to the check link. "Withdraw this certificate" asks first, then makes the check link say only "withdrawn". Getting the same content again returns the same certificate; a new one for the same nonprofit and period withdraws the old one. Refused, with a notice on Shifts and hours: no last name on the profile, hours confirmed before the last name change, or more than 20 in 24 hours. Only the owner can open these pages: another volunteer gets 404, a signed-out visitor is sent to the sign-in page, and coordinators and admins are stopped by role gating first. See 10i. | `POST /hours/orgs/{org_id}/certificate`, `GET /hours/certificate/{cert_id}`, `GET /hours/certificate/{cert_id}.pdf`, `GET/POST /hours/certificate/{cert_id}/withdraw` | `app/routes/hours.py`, `app/data.py` (`certificate_options`, `issue_certificate`, `my_certificate`, `withdraw_certificate`), `app/signing.py`, `app/certificate.py` |
| Still coming? | A day-before reminder (an email, or a text for a volunteer who added a number) ends with a link to this page, sent when `/tasks/reminders` runs (planned hourly Cloud Scheduler job, not set up yet). "Yes, I'll be there" stamps the shift once, and the coordinator then sees "Confirmed". "Can't make it" leads to the cancel page. The link carries no sign-in token. | `GET/POST /shifts/{id}/confirm` | `app/routes/shifts.py`, `app/notify.py` |
| Can't make this one | The volunteer cancels one upcoming shift, with an optional reason (up to 300 characters). On a recurring or flexible opening the match stays. On a one-time opening that is open or filled, cancelling the only upcoming shift ends the match as a withdrawal would. The org's coordinators get an email at once, without the volunteer's name. Less than 48 hours ahead, a spot given up refills itself with an urgent call for 1 person (see 10g). Linked from Matches and from the chat. | `GET/POST /shifts/{id}/cancel` | `app/routes/shifts.py`, `app/data.py` (`cancel_my_shift`), `app/notify.py` |
| Profile and photo | What nonprofits see: photo, first and last name (changing either one once it is set stamps `name_changed_at`, and the form warns that hours confirmed before the change can't go on a certificate), pronouns, short bio, up to 6 interests (chips from a list), and the neighborhood nonprofits see. Under "Only we see": an optional email (where notices go when the volunteer has no texts; it starts as the address they signed in with, and changing it doesn't change how they sign in) and any other cause they'd like, as free text. The photo is re-encoded server-side and can be removed. Delete my account is at the foot of this page. | `GET /me/profile`, `POST /me/profile/photo`, `POST /me/profile/photo/remove`, `POST /me/profile/details` | `app/routes/profile.py`, `app/photos.py` |
| Photo nudge | A prompt under the deck to add a photo, until the volunteer adds one or taps "Not now". | `POST /deck/photo-nudge/dismiss` | `app/routes/deck.py` |
| Last-minute calls | Opt-in, on `/me/preferences/urgent`. Choose same day or today-and-tomorrow, quiet hours, and up to 3 notices a week. Notices arrive by email, or by SMS for volunteers who added a number. Waves go out when `/tasks/urgent` runs (planned every 10 minutes, not set up yet). Every email carries a signed unsubscribe link that works signed out. | `GET /me/preferences/urgent`; `GET/POST /urgent/off/{key}` | `app/routes/prefs.py`, `app/notify.py`, `app/data.py` |
| Report and block | From a match, the volunteer can report the org (reason plus details) or block it. A block removes the org's openings from the deck, withdraws open matches and makes chat read-only. | `GET/POST /matches/{id}/report`, `GET/POST /matches/{id}/block` | `app/routes/safety.py` |
| Me hub, privacy, delete account | The hub links to Your profile, Preferences, Shifts and hours (each with a one-line subtitle) and the privacy policy; Sign out is a button. Deleting the account (from the profile page) soft-deletes and scrubs it. The privacy page is dated, and names the email provider in use: Resend, or Google through Gmail, with a line on Gmail's copies of sent mail (they go after a day only when `GMAIL_PURGE=daily` says the purge is installed). | `GET /profile`, `GET/POST /profile/delete`, `GET /privacy` | `app/routes/safety.py` |
| Add to Home Screen | A web app manifest and icons, plus an iPhone Safari hint on the deck. | `/static/manifest.webmanifest`, `GET /favicon.ico` | `app/routes/pwa.py` (favicon), `static/manifest.webmanifest`, `static/a2hs.js` |

### Nonprofit coordinator

| Feature | What it does | Route(s) | Code |
|---|---|---|---|
| Sign in by email link | Enter an email address to receive a single-use link that expires in 15 minutes. Only addresses of coordinators with an org membership (or of admins) get a link, and the page answers the same way for every address (a preview with the fake emailer shows a demo coordinator's link instead, and sends nothing to any other address, see 10b). The link opens a confirm page, "Sign in as {address}", with warnings when this browser didn't ask for the link or is signed in as someone else. A staff address typed at `/login` instead gets the same link. In a demo, "Try it as a nonprofit" signs in the demo coordinator in one tap and "Try it as an admin" the demo admin; elsewhere those routes answer 403. | `GET /org/login`, `POST /org/login/send`, `POST /org/login/demo`, `POST /org/login/demo-admin`, `GET/POST /org/login/verify` | `app/routes/org_auth.py`, `app/notify.py` |
| Sign in with Google or Apple | The same buttons on `/org/login`. The provider's verified email must be the coordinator's (or admin's) email on file, found as the link finds it, and the provider must host that mailbox (Gmail, a Google Workspace account whose domain matches, or iCloud); for any other address the answer is the emailed link's. Nothing is ever created: an unknown address gets a pointer to `/org/claim`. | `GET /auth/{google,apple}/start?for=org` and the callbacks | `app/routes/oauth.py`, `app/oauth.py` (`authoritative`), `app/data.py` (`staff_by_email`) |
| Ask for access (claim) | A public form: EIN, org name, the requester's name, email and optional phone, and an "I'm authorized" box. It creates a pending claim that an admin decides. Typing the org's name searches IRS records (ProPublica, California 501(c)(3)s, `static/claim.js`) and picking one fills the EIN and name; the search reads ProPublica only, never the orgs table, so it can't tell whether an org is on Causematch or claimed. Typing the EIN still works, and is the fallback when the search fails. | `GET/POST /org/claim`, `GET /org/claim/search` | `app/routes/claim.py` |
| Openings dashboard | Starts with a "Needs you" strip when anything waits: "N volunteers waiting" (to the Inbox) and "N shifts to mark" (to Shifts), each hidden at 0. Lists the coordinator's openings for each of their orgs, the open and filled ones with volunteers waiting first, with counts and any open urgent call, and a "messages to answer" link when a volunteer wrote last. A coordinator with no name yet sets their name, role and optional phone here first (after that, "Your details" lives on the Organization page). Posts new openings. Each card keeps Edit (open and filled openings) and Reopen (a closed opening whose start is still ahead) in view, and the rarer actions under "More": Photo and event link, the link to post an urgent call, and Close opening (an open one). | `GET /org`, `POST /org/details`, `GET/POST /org/openings/new`, `POST /org/openings/{id}/close`, `POST /org/openings/{id}/reopen` | `app/routes/org.py`, `app/data.py` (`reopen_opening`) |
| Opening form | Title, about, cause, ZIP (LA County; the neighborhood fills in from a local table, `static/zip.js`), neighborhood, next start (within 12 months), how long a shift is (1 to 8 hours), recurrence (one-time / weekly / monthly / flexible), slots (1 to 200), orientation, a preferred reply channel (stored; notices are email-only today), up to 3 skills, kind of work, helpful languages, notes, and a required confirmation that the opening is adults-only with no one-to-one contact. | `/org/openings/new`, `GET /org/zip` | `app/routes/org.py` |
| Edit and reopen an opening | "Edit" opens the same form prefilled and saves to that opening, so its likes and matches stay. Always editable: title, what you'll do, notes, spots (never below the volunteers already accepted), orientation, kind of work and the reply channel. Shown but fixed: the organization, the place and the repeat, always; the date, time and length, and the cause, once anyone has liked the opening (every match counts, a demo copy's too, `data.opening_locks`), because likers chose it for that time and the deck ranked it by that cause. An opening with more than one cause keeps them. A closed opening can't be edited: "Reopen" it first, which works while its start is ahead and sets it open or filled from the volunteers accepted (shifts cancelled by the close stay cancelled). Each save that changes something volunteers rely on writes one line into the chat of every accepted volunteer, naming the fields in their words ("{org} updated this opening: spots, title."). It names only the always-editable fields, because an accepted volunteer means the date, time and cause are locked. It is not written into a chat where a block stands either way. Changing only the reply channel tells no one. The line is the app's own message (`data.SYSTEM_SENDER`, sender 0): shown as a quiet centered note, with no sender, never sent as a notice, and it does not count as the volunteer's turn to be answered. Saves are throttled per coordinator: 10 in a row, then one a minute. | `GET/POST /org/openings/{id}/edit`, `POST /org/openings/{id}/reopen` | `app/routes/org.py` (`OpeningForm`, `opening_input`, `opening_error`), `app/data.py` (`update_opening`, `opening_locks`, `reopen_opening`), `app/routes/chat.py` |
| Inbox, accept and decline | Starts with "Messages to answer" when there are any: accepted chats whose latest message from a person is the volunteer's (the app's own opening-update lines don't count). A reply from anyone at the org clears it, and blocked, withdrawn or deactivated chats never wait. Then groups likes into "Waiting for you", "Decided" and "Past matches". Each row shows only what the volunteer agreed to share, and a waiting one also the shift the like is for (date and time, spots left) and whether it falls in their usual times, as the weekday or weekend part of the day, never which days. Accept is the button; Decline is plain text beside it, because a mis-tap costs one Undo: right after a decline the inbox shows "Declined. {name} hasn't been told yet" with an Undo button, good for two minutes (and refused once the opening has closed), during which the volunteer sees nothing of it. Accepting books the first shift (when its start is still ahead) and tells the volunteer, after the response. | `GET /org/inbox`, `POST /org/matches/{id}/decide`, `POST /org/matches/{id}/undo` | `app/routes/org.py`, `app/data.py` (`org_inbox`, `org_unanswered`, `decide`, `undo_decline`) |
| Chat | The coordinator side of the same chat, with the shift line and the volunteer's usual times (weekday or weekend, by part of the day) in its header. | `GET /org/chat/{id}`, `POST /org/chat/{id}/send`, websocket `/org/chat/{id}/ws` | `app/routes/chat.py` |
| Shifts and attendance | Three lists: "Did they come?" (started, not marked), "Marked this week" and "Upcoming". An upcoming shift reads "Confirmed" once the volunteer said they're coming. Marking a started shift Attended confirms its hours: the choice is pre-set to the shift's length, and the coordinator can pick 30 minutes to 12 hours, but no more than the longer of the shift's length and the time since it started; a shift with no length asks for its hours. No-show confirms none. One correction (of the result or the hours) is allowed within 7 days. The coordinator can also cancel a future shift, add another shift (with its own length), or repeat "same time next week" for weekly openings. | `GET /org/shifts`, `POST /org/shifts/{id}/mark`, `POST /org/shifts/{id}/cancel`, `GET/POST /org/matches/{id}/shifts/new`, `POST /org/matches/{id}/shifts/repeat` | `app/routes/shifts.py`, `app/data.py` (`complete_shift`) |
| Org profile and media | Org about text and website (https only), a "We can confirm volunteer hours" box (off by default), a logo and a photo of the org's work, and the coordinator's own details. Each opening can have its own photo and a link to the org's own event page. | `GET /org/organization`, `POST /org/organization/{org_id}`, `.../{org_id}/logo`, `.../{org_id}/photo`, `GET /org/openings/{id}/media`, `POST /org/openings/{id}/photo`, `POST /org/openings/{id}/link` | `app/routes/org_profile.py`, `app/photos.py` |
| Urgent calls (last-minute calls on screen) | From "Short of people? Post a last-minute call" under "More" on an open opening's card, for a shift in the next 48 hours, the coordinator posts "need N more" on an open opening, optionally auto-accepting the first N likes. Limit: 3 calls per org per 24 hours. A call can be stopped early. | `GET/POST /org/openings/{id}/urgent`, `POST /org/openings/{id}/urgent/close` | `app/routes/org.py`, `app/data.py`, `app/notify.py` |
| Report and block a volunteer | Report or block a volunteer from a match. A block declines pending likes, withdraws an accepted match and silently drops the volunteer's future likes. | `GET/POST /org/matches/{id}/report`, `GET/POST /org/matches/{id}/block` | `app/routes/safety.py` |
| Email notices | "New volunteer interest" (at most one per org per 15 minutes), "a volunteer was accepted automatically" for urgent calls, "a volunteer can't make" a shift (with their reason, and whether an urgent call went out or could be posted), and "a volunteer withdrew" from an accepted match. The last two never name the volunteer: the link does. All go out after the response is out. | sent by `app/notify.py` | `app/notify.py` |

### Admin

| Feature | What it does | Route(s) | Code |
|---|---|---|---|
| Sign in | Admins use the same emailed link, or Google or Apple by verified email, as coordinators and land on `/admin`, the admin home. They can't open volunteer or `/org` pages. In a demo (`CAUSEMATCH_DEMO_MATCHES=1`), "Try it as an admin" on `/org/login` signs in the demo admin in one tap, with a seeded claim to approve. | `/org/login`, `POST /org/login/demo-admin`, `GET /admin` | `app/routes/org_auth.py`, `app/auth.py` |
| Org verification | An org's openings stay out of every deck until it is verified. The admin looks the EIN up on ProPublica Nonprofit Explorer, then verifies. | `GET /admin/orgs`, `POST /admin/orgs/{id}/lookup`, `POST /admin/orgs/{id}/verify` | `app/routes/admin.py`, `app/propublica.py` |
| Claims | Pending access requests as cards (org, city, EIN, the person, their email) with Approve and Decline, and a one-line confirmation after each. If no org has the claim's EIN, Approve imports it from ProPublica first (as `scripts/import_orgs.py --ein` does), in the same tap; when ProPublica fails, the card says so and Approve retries. Approving makes the requester the owner (or a coordinator if the org already has an owner) and verifies the org. If the org already has people who can sign in, the admin must confirm adding another. When the typed name differs from the IRS name, the card shows both and Approve needs a "Yes, approve for {IRS name}" box ticked. When a volunteer signs in with the claim's exact address, the card warns that approving makes it a coordinator sign-in and Approve needs a "Yes, make it a coordinator sign-in" box ticked (10a). Approval is refused for a deactivated org, a suspended coordinator's address or a suspended volunteer's mailbox. Both decisions are emailed. | `GET /admin/claims`, `POST /admin/claims/{id}/approve`, `.../reject` | `app/routes/admin.py`, `app/org_import.py` |
| Reports queue | Dismiss a report, suspend the reported volunteer, deactivate a reported org, or remove a reported volunteer's photo. | `GET /admin/reports`, `POST /admin/reports/{id}/dismiss`, `.../suspend`, `.../deactivate`, `.../remove-photo` | `app/routes/admin.py`, `app/data.py` |
| Blocks | Lists blocks and lets the admin undo one. | `GET /admin/blocks`, `POST /admin/blocks/{id}/unblock` | `app/routes/admin.py` |
| Pilot metrics | Aggregates only: like rate, accept rate, reply within 48 h, first and repeat shifts, no-shows, empty decks and last-minute call figures. Broken down by cause, by how the org was introduced, and by how the volunteer heard of us. Small groups are merged and thin rates show as "too few". Downloadable as CSV. | `GET /admin/metrics`, `GET /admin/metrics.csv` | `app/metrics.py`, `app/metrics_rows.py` |

`docs/ADMIN_GUIDE.md` walks an admin through these pages, in order.

Some admin tasks are command-line scripts in `scripts/`:

- `add_admin.py`, `remove_admin.py`: grant or revoke admin access (`add_admin.py` refuses a volunteer's sign-in address without `--force`, see 10a).
- `add_coordinator.py`, `remove_coordinator.py`: grant coordinator access or delete a coordinator (`add_coordinator.py` refuses a volunteer's sign-in address without `--force`, see 10a).
- `verify_org.py`: look an EIN up on ProPublica and, with `--org-id N --approve`, verify that org.
- `import_orgs.py`: import nonprofits from ProPublica.
- `stats.py`: print headline numbers from a read-only database.

### Anyone with a check link

| Feature | What it does | Route(s) | Code |
|---|---|---|---|
| Check a certificate | Public, signed in or not. A genuine certificate shows "Genuine certificate", or "Hours have changed" when the nonprofit has corrected any of its shifts since, with the volunteer's full name, the nonprofit and its EIN, the period, the total, the issue date and the signature code. A withdrawn one says only "This certificate was withdrawn." An unknown id, a garbled one, or a stored certificate whose signature no longer matches all get the same 404 "Not a Causematch record". Limited to 30 per 10 minutes per address. Never indexed. | `GET /check/{code}` (the code is the certificate's id) | `app/routes/hours.py` (`check_link`, `CheckPage`), `app/data.py` (`check_certificate`) |

---

## 7. Request pipeline

`main.py` builds the app with `fast_app(...)`, passing `Guard`, `PreviewGate` and `RunAfter` as user middleware. FastHTML places user middleware ahead of its own session middleware, and Starlette runs the first middleware in the list outermost. The resulting order, inside Starlette's own `ServerErrorMiddleware` (outermost), with Starlette's `ExceptionMiddleware` (the 404 page) between the session and the router, is:

```mermaid
flowchart TD
    req(["HTTP request or websocket handshake"]) --> guard
    guard["<b>security.Guard</b><br/>same-origin check on POST/PUT/PATCH/DELETE, exempt: /tasks/reminders, /tasks/urgent, /auth/apple/callback<br/>websocket: exact Origin, else close 1008<br/>body limit 64 KB, 6 MB on photo uploads<br/>adds CSP and security headers, X-Request-Id<br/>production: plain 500 page with a reference"]
    guard --> gate
    gate["<b>security.PreviewGate</b><br/>preview only: access cookie or HTTP Basic password on every request<br/>?access=key on a navigation sets the cookie, 303 without the key<br/>else a plain 401 page, no WWW-Authenticate<br/>/robots.txt answered here with Disallow: /<br/>wrong guesses limited per client address"]
    gate --> after["<b>notify.RunAfter</b><br/>HTTP: after the response is out, runs what handlers queued with notify.after<br/>websocket: each at once, in a task group of the socket's own, beside its frames<br/>each in a worker thread, at most 4 at once, apart from the shared pool<br/>a failure is logged, never raised"]
    after --> sess["Starlette SessionMiddleware<br/>signed cookie: __Host-cm_session in production and preview, cm_session in dev"]
    sess --> exc["Starlette ExceptionMiddleware<br/>404 page: app/routes/safety.py not_found"]
    exc --> router{"Router"}
    router -- "/static/*" --> static["GZip + StaticFiles<br/>htmx, Franken UI, daisyUI, theme, JS"]
    router -- "websocket /chat/{id}/ws, /org/chat/{id}/ws" --> ws["chat ws handlers<br/>check session and role themselves:<br/>Beforeware does not run for websockets"]
    router -- "page and form routes" --> bw
    bw["Beforeware 1, inside the endpoint wrapper: <b>auth.check_session</b><br/>skips the PUBLIC paths<br/>live profile and session version, then role gating"]
    bw --> thr["Beforeware 2: <b>security.throttle</b><br/>per-user token buckets on swipe, chat, report/block, opening-edit and upload POSTs"]
    thr --> handler["Route handler, app/routes/*.py<br/>sync work runs in a threadpool"]
    handler --> data["app/data.py<br/>every public function under one lock"]
```

### Auth, sessions and roles (`app/auth.py`)

- **Public paths** skip the sign-in check: `/login`, `/login/send` (the emailed link, rate-limited), `/login/verify` (GET shows the confirm page, POST uses the link), `/login/demo`, `/auth/*` (Google and Apple sign-in, 404 when not configured), `/org/login`, `/org/login/send`, `/org/login/verify`, `/org/login/demo`, `/org/login/demo-admin`, `/org/claim`, `/org/claim/search`, `/privacy`, `/tasks/reminders`, `/tasks/urgent`, `/urgent/off/{key}`, `/check/{code}` (a certificate's check link), `/favicon.ico` and `/static/*`.
- **Session contents:** `uid` and `sv`, the session version. A request is signed in only if the profile still exists and isn't deleted or suspended, and `sv` equals the profile's `session_version`. Bumping that version signs out every device; sign-out, suspension and account deletion bump it. A Google or Apple connect from Me is sealed with it, so one started before a sign-out is refused. A deleted or suspended profile fails the check anyway, so removing an admin or coordinator also takes effect at once.
- **Role gating:**
  - `/admin/*`: admins only. A coordinator who tries is redirected to `/org`, anyone else gets 403.
  - `/org/*`: coordinators with at least one current org membership, checked on every request. Others get 403.
  - Volunteer pages: volunteers only. A coordinator is redirected to `/org`, and an admin gets 403.
  - A volunteer who hasn't finished onboarding is redirected to `/onboarding` (`/logout` stays reachable).
  - `/photos/*` is open to every signed-in role. `app/data.py` then decides who may see each photo (see section 12).
- **Websockets** apply the same rules through `auth.ws_session_user`, on connect, on every frame and before each push. It also refuses a volunteer who hasn't finished onboarding.
- Unmatched paths reach the 404 handler without running any Beforeware.
- Ownership of a single row is checked in `app/data.py`, not here. For example the pages that act on one shift (`/shifts/{id}/cancel`, `/shifts/{id}/confirm`, `/shifts/{id}.ics`) go through `data.shift_event`, which accepts only the volunteer's own upcoming shift on an accepted match; anything else answers 404. The certificate pages go through `data.my_certificate`, and withdrawing through `data.withdraw_certificate`, which check ownership the same way.

### htmx partials and full pages

Most pages are plain server-rendered HTML with ordinary form POSTs and redirects, so they work without JavaScript. htmx is used in three places:

- **Swiping.** The Like/Pass form posts with `hx-post="/swipe"` and swaps `#deck-area` with the next card. `POST /swipe` checks `HX-Request`: for htmx it returns the partial, or an `HX-Redirect` to the chat when an urgent call auto-accepted the like, or to `/onboarding/more` after the volunteer's first like (the two optional questions). The partial also carries the Matches number as an out-of-band swap, and any new "It's a match!" takeover. Without htmx it redirects to `/deck`.
- **The match moment.** On the deck and Matches pages, `#moment-slot` asks `GET /deck/news` every 10 seconds and when the tab becomes visible again (`static/sheet.js` skips a hidden tab). A new match comes back as the takeover; with nothing new the answer is only the Matches number, with `HX-Reswap: none`, so a takeover already open stays.
- **Chat.** The page connects with htmx's `ws` extension. The server pushes each new message to every open socket on that match as an out-of-band swap appended to `#messages`. `static/chat.js` reloads from the server's copy when the socket reconnects after a drop, or when the server refuses it with 1008. Each user may hold 5 sockets per match and 20 overall, and each frame takes a token from the chat bucket (close code 1013 when empty). `POST /chat/{id}/send` is the no-script fallback.

htmx, its ws extension, Franken UI and daisyUI are vendored under `static/vendor/`. `htmx-config` turns off eval and script tags.

---

## 8. Module map

The main imports, one layer at a time. Arrows read "imports / calls". Route modules never touch `app/db.py` directly: all reads and writes go through `app/data.py`. Left out to keep it readable: `app/config.py` (environment rules, read by most modules), `app/ui.py` and the other shared helpers every route uses, `main.py` (which assembles everything), `app/demo_seed.py`, smaller cross-imports (`app/notify.py` uses `app/security.py`, `app/oauth.py` uses `app/auth.py`, `app/security.py` uses `app/photos.py`), and one lazy import from `app/notify.py` back to `app/routes/chat.py` (`volunteer_online`).

```mermaid
%%{init: {"flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
flowchart TD
    subgraph routes["app/routes/"]
        r_vol["Volunteer<br/>auth, oauth, onboarding, deck, matches,<br/>prefs, profile, chat, shifts, hours, safety"]
        r_org["Nonprofit<br/>org_auth, claim, org, org_profile"]
        r_adm["Admin and other<br/>admin, tasks, photos, pwa"]
    end

    gate["auth.py, security.py<br/>sessions, roles, Guard, rate limits"]
    oauth["oauth.py<br/>Google, Apple, the fake"]
    notify["notify.py<br/>texts, email, reminders, waves"]
    data["data.py<br/>domain logic, one lock"]
    side["metrics.py, propublica.py,<br/>org_import.py"]
    pure["match.py, prefs.py<br/>scoring, reading preferences"]
    photos["photos.py<br/>validate, re-encode"]
    cert["signing.py, certificate.py<br/>HMAC signatures, PDF, QR code"]
    worker[["photo_worker.py<br/>child process"]]
    db[("db.py, migrations/<br/>tables, numbered steps")]

    r_vol & r_org & r_adm --> data
    r_vol & r_org & r_adm --> notify
    r_vol & r_org & r_adm --> gate
    r_vol --> oauth
    r_vol & r_org --> pure
    r_org & r_adm --> side
    r_vol & r_org --> photos
    r_vol --> cert
    oauth --> photos
    notify --> data
    gate --> data
    data --> pure
    data --> photos
    data --> cert
    data --> db
    photos --> worker

    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    class r_vol,r_org,r_adm,gate,oauth,notify,side,pure,photos,cert step
    class data,db,worker key
```

| Module | Responsibility |
|---|---|
| `main.py` | Startup refusals (environment, secrets, no email sender at all (one is always required, the fake where allowed), providers, Twilio and SMTP set halfway), uvicorn settings, `fast_app` with middleware and Beforeware, the static mount, router registration, seeding. |
| `app/config.py` | What each environment allows: `is_production`, `is_preview`, `is_dev`, `fakes_allowed`, plus required-value checks for every secret and setting (`require_volunteer_sign_in`, the SMTP settings in `smtp` and `smtp_max_per_day`, `gmail_purge`) and what a preview lets in (`demo_address`, `demo_number`). Has no imports from `app`. |
| `app/security.py` | `Guard`, `PreviewGate`, CSP and headers, body limits, client IP from `X-Forwarded-For`, persistent rate limits (`rate_event` table, including `link_limits` for emailed links), in-memory token buckets, error pages, and the access-log token redaction. |
| `app/auth.py` | Session secret and cookie name, phone normalization (for Me's add-a-number), `sign_in` and `sign_in_staff`, the Beforeware sign-in and role check, websocket session check. |
| `app/routes/auth.py`, `app/routes/org_auth.py` | The emailed links: the volunteer's (`/login`) and the staff's (`/org/login`), their shared confirm page (`ConfirmPage`), and the demo buttons. |
| `app/oauth.py`, `app/routes/oauth.py` | Google and Apple sign-in, and the fake that stands in for both: the flow cookie, PKCE, Apple's client secret, ID token verification, the Google picture fetch, a preview browser's own fake subject; the start, callback, connect and disconnect routes (the callback refuses a staff mailbox and marks the fake untrusted) and the fake consent page. |
| `app/data.py` | All domain logic and every query the routes and services run, apart from the metrics queries (`app/metrics_rows.py`). Schema statements live in `app/db.py` and `app/migrations/`, and `app/demo_seed.py` writes seed rows through `app/db.py`'s tables. Covers the deck, swipes, matches (a decline's undo window included), openings' edits and reopening, shifts and confirmed hours, certificates (issuing, withdrawing, the check), chat, urgent calls (refills included), notification bookkeeping, reports and blocks, claims, emailed-link tokens, which mailboxes may be sent one, joining a first sign-in to the volunteer who holds its address, photos' access rules, account deletion and rate-event storage. Every public function runs under one re-entrant lock because a single apsw connection is shared by all threads. |
| `app/match.py` | Pure ranking functions: distance and travel time, cause fit, availability, work fit, language fit, and the weighted score. Has no database access. |
| `app/prefs.py` | Reads a volunteer's preferences consistently whatever the profile's age, and resolves a typed place (ZIP, neighborhood, device location) to a point. |
| `app/notify.py` | The senders and their fakes: Twilio Verify (Me's add-a-number codes), Twilio SMS, and email through Resend (`ResendEmailer`) or a Gmail account over SMTP (`SMTPEmailer`: TLS only, a deadline per send, a pause after a failed connect, one connection per `batch()`, and an `EmailCeiling` of `SMTP_MAX_PER_DAY` split into shares by kind). `RunAfter` and `after()` run notices and sign-in links once the response is out, or, from a chat socket, beside its frames. `channel` picks text or email per volunteer. Sends exactly-once notifications recorded in the `notification` table, day-before reminders, urgent-call waves, and the emails to an org when a volunteer cancels a shift or withdraws. |
| `app/signing.py` | Certificate signatures: HMAC-SHA256, keyed by `config.certificate_key` (`CERTIFICATE_KEY`), over a canonical JSON encoding of the certificate's id and snapshot (`canonical`, `sign`, `verify` with a constant-time compare), and the 16-character signature code people read (`code`). No database access. |
| `app/certificate.py`, `app/fonts/` | The certificate PDF (`build_pdf`): US Letter, from the stored snapshot, with the signature code on every page and a QR code of the check link, drawn with fpdf2 and segno. Text is set in Noto Sans (embedded as a subset; `app/fonts/` is outside `static/`, so it is never served), and any character the font lacks, or any control or format character, prints as "?" (`pdf_text`, using fontTools). Also the filename and the date and EIN labels the pages share. No database access. |
| `app/photos.py`, `app/photo_worker.py` | Upload validation, the sandboxed decode in a child process, and re-encoding to JPEG (full and thumbnail). |
| `app/db.py`, `app/migrations/` | Table classes (fastlite `db.create`, which only makes missing tables), indexes, the cause rows, invented demo orgs, and numbered migrations, which alone change an existing table. `migrations.check` runs after the tables are declared. |
| `app/metrics.py`, `app/metrics_rows.py` | Pilot metrics: plain-row queries, then pure aggregation. No names, phones or emails. |
| `app/propublica.py`, `app/org_import.py` | ProPublica API client, and the import rules: LA County ZIPs and NTEE code mapped to a cause. |
| `app/demo_seed.py` | Demo and preview data: invented orgs, or orgs from a seed file kept outside git, with "Sample:" openings and demo people. Beside a seed file it adds one invented demo nonprofit, the coordinator's, which confirms hours. `demo_volunteer` makes each "Try it as a volunteer" copy of Sam, who arrives with a match accepted 15 days ago and a completed shift with confirmed hours (`data.arrive_with_history`), so a certificate works in one visit. It also seeds an unverified nonprofit with a pending claim for the demo admin to approve (an invented nonprofit and claimant, so approving never asks ProPublica). |
| `app/ui.py`, `app/forms.py`, `app/choices.py`, `app/geo.py` | Shared components and routers (the nav tabs, one label per match and shift state, time ranges, calendar links), form helpers (the email address shape, and `abuse_key`, the mailbox behind an address), answer lists (shift lengths, confirmable hours), and LA County ZIP and neighborhood lookups (`app/la_zips.csv`, `app/la_neighborhoods.csv`). |

---

## 9. Data model

There is one SQLite file (`CAUSEMATCH_DB`, default `data/causematch.db`), opened with fastlite and apsw. Each table except `schema_migrations` is a Python class in `app/db.py`. The schema declares **no foreign keys**: the relations below are enforced in `app/data.py`. Times are ISO strings, except `rate_event.at` (unix seconds): `created_at`-style stamps are UTC, while `starts_at` on openings, shifts and urgent calls is Los Angeles wall-clock time.

```mermaid
erDiagram
    org ||--o{ opening : posts
    org ||--o{ org_member : has
    profile ||--o{ org_member : "coordinator in"
    profile ||--o{ place : "helps from, max 2"
    profile ||--o{ swipe : makes
    opening ||--o{ swipe : receives
    profile ||--o{ match : "volunteer in"
    opening ||--o{ match : "liked as"
    match ||--o{ message : "chat of"
    match ||--o{ shift : books
    opening ||--o{ urgent_call : "short of people"
    urgent_call |o--o{ match : "likes counted toward"
    profile |o--o{ login_token : "staff link for"
    profile ||--o{ identity : "signs in with"
    profile ||--o{ report : files
    profile ||--o{ block : "blocks or is blocked"
    org ||--o{ block : "blocked or covered"
    claim_request }o--o| org : "approved into"
    profile ||--o{ deck_empty : "saw an empty deck"
    profile ||--o{ certificate : "issues"
    org ||--o{ certificate : "confirmed the hours on"

    profile {
        int id PK
        string role "volunteer, coordinator, admin"
        string phone "E.164, optional, added in Me for texts only with CAUSEMATCH_COLLECT_PHONE=1"
        string email "sign-in or notice address"
        string display_name
        string last_name
        string name_changed_at "last rename, UTC"
        int session_version
        string cause_prefs "JSON slug to love, open, no"
        string availability "JSON day slots"
        string free_dates "JSON dates"
        string dealbreakers "JSON"
        int weekly_limit "1 to 5, 0 or null off"
        string verified_hours "only or null, an old prefer reads as off"
        bool urgent_on
        string photo_key
        string onboarded_at
        string deleted_at
        string suspended_at
        bool sms_opted_out
    }
    org {
        int id PK
        string name
        string ein "unique"
        string verified_at
        string deactivated_at
        string causes "JSON slugs"
        string intro "warm or cold"
        bool confirms_hours
        string logo_key
        string photo_key
    }
    opening {
        int id PK
        int org_id
        string title
        string causes "JSON slugs"
        float lat
        float lon
        string starts_at "LA wall clock"
        int duration_minutes "shift length"
        string recurrence
        int slots
        string status "open, filled, closed"
        string setting
        string effort
        string group_size
        string languages "JSON"
        string event_url
    }
    org_member {
        int id PK
        int org_id
        int user_id
        string role "owner or coordinator"
    }
    place {
        int id PK
        int user_id
        float lat "centroid or rounded"
        float lon
        string mode "car, transit, bike, walk"
        int max_minutes "0 means any"
    }
    swipe {
        int volunteer_id PK
        int opening_id PK
        bool liked
    }
    match {
        int id PK
        int opening_id
        int volunteer_id
        string status "pending, accepted, declined, withdrawn"
        bool org_blind
        int urgent_call_id
        string undo_until "UTC, a decline can be undone until then"
    }
    message {
        int id PK
        int match_id
        int sender_id "0 is the app's own line"
        string body "max 2000 chars"
    }
    shift {
        int id PK
        int match_id
        string starts_at "LA wall clock"
        int duration_minutes "the opening's, or picked when added"
        string status "scheduled, completed, no_show, cancelled"
        string kind "accept or added"
        string confirmed_at "Still coming, yes"
        int confirmed_minutes "hours confirmed"
        int marked_by
        int corrected_by
        int cancelled_by "coordinator or volunteer"
    }
    urgent_call {
        int id PK
        int opening_id
        int org_id
        string starts_at
        int needed
        bool auto_accept
        string status "open, filled, closed"
        int waves "0 to 2"
        bool refill "posted by a late cancel"
    }
    login_token {
        string token_hash PK "SHA-256"
        int user_id "staff link, NULL for a volunteer's"
        string email "volunteer link, lowercased"
        string expires_at
        string used_at
    }
    report {
        int id PK
        int reporter_id
        int target_user_id
        int target_org_id
        string reason
        string status "open, dismissed, actioned"
    }
    block {
        int id PK
        int blocker_id
        int blocked_user_id
        int blocked_org_id
    }
    claim_request {
        int id PK
        string ein
        string email
        string status "pending, approved, rejected"
        int org_id
    }
    identity {
        int id PK
        int user_id
        string provider "google, apple or email"
        string subject "unique per provider, the address for email"
        string email "as the provider gave it"
        int authoritative "1 holder, 2 joiner, 0 or NULL neither"
    }
    deck_empty {
        int id PK
        int volunteer_id
        bool today
    }
    certificate {
        string id PK "22 random chars, the check link"
        int volunteer_id
        int org_id
        string period "2026-10 or all"
        int minutes "the total"
        string issued_at
        string withdrawn_at
        string snapshot "JSON, NULL once withdrawn"
        string signature "HMAC-SHA256 hex, NULL once withdrawn"
    }
```

These tables stand on their own:

| Table | Holds |
|---|---|
| `cause` | The four causes (slug, label, icon, sort, active). Openings, orgs and profiles refer to causes by slug inside JSON lists. |
| `photo` | Re-encoded JPEG bytes (`full`, `thumb`) keyed by a random 22-character `key`. `owner_kind` is one of `volunteer`, `org_logo`, `org_photo`, `opening`, with `owner_id` pointing at the matching row. The bytes live in SQLite, so a database backup covers the photos too. |
| `notification` | One row per logical send. `kind` is one of `accepted`, `message`, `likes`, `reminder`, `urgent`, `auto`, `cancel`, `withdrawn`. `key` is unique (for example `accepted:{match}`, `reminder:{shift}`, `urgent:{call}:{volunteer}`, `cancel:{shift}`, `withdrawn:{match}`). `status` is one of `pending`, `sent`, `failed`, `skipped`, `cancelled`, `deferred` (held back by the SMS ceiling) or `unknown` (timed out). This is what makes notices exactly-once and retryable. |
| `rate_event` | One counted action for the persistent rate limits and the email ceilings. The key is the limit name plus an HMAC of the phone, mailbox or IP, never the value itself. Rows older than 24 h are pruned. |
| `retained_phone_hash` | An HMAC of a deleted account's phone, kept only when a block, report or suspension must survive deletion: a number a suspended account left can't be added to another one, and the org blocks apply to a profile that adds it. |
| `retained_identity_hash` | The same for each sign-in identity the deleted profile had: HMAC-SHA256(`PHONE_HASH_KEY`, "provider:subject"), on the same terms, re-applied when that account signs up or is connected again. For the emailed link the subject is the mailbox (`forms.abuse_key`: lowercased, without a `+tag`, and for Gmail without dots), and a deleted Google or Apple account leaves one for its verified address too, so a suspension or block follows a later sign-up to that mailbox by any way in. A disconnect keeps one too when a block, a report or a suspension stands against the profile. |
| `schema_migrations` | Applied migration versions. |

`identity.authoritative` says whether the identity's email may be used to join accounts, and is stored again at every sign-in. `data.HOLDER` (1): the address may hold an account that another way in joins: an emailed link's (always), or Gmail or googlemail on Google, or iCloud, me.com or mac.com on Apple, judged by the address alone. `data.JOINER` (2): Google vouches only as a Workspace's, whose `hd` is the address's domain; it proves the mailbox now, so it may join into a holder, but it holds nothing, because a Workspace may give the address to someone else later while Google keeps it on the old account. 0 or NULL: neither (any other address, an unverified one, the fake provider's on a preview). When looking for the account to join, an emailed-link row matches by its address (its subject), whatever this column says, and any other row only when it is a HOLDER.

Notable indexes, created in `app/db.py`:

- `swipe` primary key `(volunteer_id, opening_id)`: one swipe per card.
- `identity_provider_subject` unique: one profile per Google or Apple account, or per emailed-link address.
- `match (opening_id, volunteer_id)` unique: one match per like.
- `notification(key)` unique.
- `shift_match_start_live`: one live shift per match and start time.
- `profile_phone_live`: one live profile per phone.
- `coordinator_email_live` and `admin_email_live`: one live profile per email and role.
- `block_once_v2`: one block per side.
- `idx_certificate_volunteer_id_issued_at` on `certificate (volunteer_id, issued_at)`: read by the limit of 20 certificates per volunteer in 24 hours.

A certificate's `snapshot` is the JSON of what it says, frozen when it was issued: the volunteer's full name, the nonprofit (name, EIN, website), the coordinators who confirmed the hours, each shift (its id, date, role and minutes), the total, the period and the issue date (Los Angeles date). The row's `signature` is computed over that snapshot and the id (section 12). Withdrawing a certificate, issuing a new one for the same nonprofit and period, or deleting the account stamps `withdrawn_at` and sets `snapshot` and `signature` to NULL, leaving only `id`, `volunteer_id`, `org_id`, `period`, `minutes` and the dates. The kept `id` is what lets the check link say "withdrawn".

`profile.name_changed_at` is stamped whenever a volunteer changes a first or last name that was already set (`data._renamed`, called from `save_onboarding` and `save_profile_details`). Filling a name in for the first time doesn't count, and a later Google or Apple sign-in never renames anyone. It exists for certificates: `issue_certificate` refuses hours confirmed at or before that moment, so one person's hours can't be certified under two names.

### Migrations

For new changes, migrations are the only thing that changes an existing database's schema (`app/migrations/__init__.py`):

1. **Adding a table:** edit `app/db.py`. `db.create` makes a table that doesn't exist yet and never rebuilds or alters one.
2. **Adding a column, renames, data changes, index changes on existing tables, and anything involving triggers:** add a numbered module `app/migrations/NNNN_what.py` with `up(conn)`, and also change the class to the end state. A new column is an `ALTER TABLE ... ADD COLUMN`, guarded so it does nothing when the column is already there.

That is the policy for new changes. `app/db.py` still runs a few idempotent statements at every start, which do not reshape a table: it drops one legacy trigger (`shift_with_match`) and a few legacy indexes (`block_once`, `idx_shift_match_id_starts_at`, `idx_profile_phone`), and creates its indexes with `CREATE INDEX IF NOT EXISTS`.

Pending migrations run in order at every start, before the tables are declared. Each runs in its own transaction together with its `schema_migrations` row. A brand-new database records all migrations as applied without running them. You can also run them by hand with `uv run python -m app.migrations [--db PATH]`.

After the tables are declared, `migrations.check` compares each with its class. A table that lacks a column its class declares stops the app, naming the migration to write. A column only the table has is kept, never dropped, and logged as a warning. It compares names only: a changed type, primary key or NOT NULL needs a migration all the same.

Why `db.create` no longer reshapes tables: with `transform=True`, every start copied ten tables into `<table>_new_<hex>` and renamed them back, because a bool field never compares equal to the integer SQLite reads back. It also dropped any column a class no longer declared, and its rename stranded rows once when a trigger named a dropped table.

Current migrations:

- `0001_baseline`: a marker only.
- `0002_homelessness_active`: turns the homelessness cause on.
- `0010_cause_prefs`: picked causes become "love".
- `0011_day_slots`: weekday/weekend buckets become day slots.
- `0012_shift_confirmed`: `shift.confirmed_at`, stamped when the volunteer answers "Still coming?" with yes.
- `0013_weekly_limit`: `profile.weekly_limit`, "Up to N shifts a week".
- `0014_shift_length`: `duration_minutes` on `opening` and `shift`. Older rows keep NULL (no length known).
- `0015_confirmed_hours`: `shift.confirmed_minutes`, the hours a coordinator confirmed. Shifts marked before it keep NULL.
- `0016_verified_hours`: `org.confirms_hours` and `profile.verified_hours`. Every org starts as not confirming, every volunteer with no preference. The volunteer's value was then `prefer` or `only`; since the checkbox only `only` counts, and a stored `prefer` reads as off (`prefs.verified_hours`).
- `0017_certificates`: the `certificate` table and its `(volunteer_id, issued_at)` index, both `IF NOT EXISTS`.
- `0018_name_changed_at`: `profile.name_changed_at`. NULL means the name was never changed, so hours confirmed before this migration can all be certified.
- `0019_match_undo_until`: `match.undo_until`, until when a coordinator's Decline can be taken back. NULL means the decision was never undoable.
- `0020_transform_columns`: the columns that `transform=True` had added from the classes with no migration of their own, among them `login_token.email` for the volunteers' emailed link. The list is frozen, not read from the classes, so the migration keeps meaning the same thing as the classes change. Each column is added only if missing, so a database last started long ago still starts.
- `0021_identity_authoritative`: `identity.authoritative`. Existing rows are judged by their address: HOLDER (1) for emailed-link rows, Gmail and googlemail rows on Google, and iCloud, me.com and mac.com rows on Apple; Workspace rows stay NULL, because `hd` was never stored, so they hold nothing, as a JOINER would. The next sign-in by each identity sets the column anew.

A new table needs only its class (the `identity` and `retained_identity_hash` tables came that way). Everything else, a new column included, needs a migration, and `check` is what catches one that was forgotten. `tests/fixtures/schema_since_baseline.sql` holds every table as `app/db.py` first created it since the baseline, and `tests/test_security.py` migrates that and expects the columns of a new database.

---

## 10. Key flows

### Match lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending: volunteer likes
    [*] --> accepted: like on an auto-accepting urgent call with places left
    pending --> accepted: coordinator accepts, a slot is free
    pending --> declined: coordinator declines, or blocks the volunteer
    declined --> pending: coordinator undoes the decline within 2 minutes
    pending --> withdrawn: volunteer withdraws, blocks the org, deletes the account, or is suspended
    accepted --> withdrawn: volunteer withdraws, either side blocks, org deactivated, account deleted or suspended
    accepted --> withdrawn: volunteer cancels the only upcoming shift of an open or filled one-time opening
    note right of accepted
        chat open. First shift booked if its start is ahead.
        Volunteer told (email, or text) when a coordinator accepted.
        An auto-accepted volunteer is redirected to the chat instead.
    end note
```

Details the diagram doesn't show:

- A coordinator's decline is held for two minutes (`match.undo_until`): the coordinator can undo it, which makes the match `pending` again, and the volunteer is shown the like as still waiting, and the Matches number doesn't count it until the window closes. An org deactivation turns pending matches into `declined`.
- When a match leaves `accepted`, its future shifts are cancelled and the opening switches back from `filled` to `open` if a slot freed up.
- Closing an opening closes its urgent call and cancels its future shifts. Its matches keep their status. Reopening a closed opening whose start is ahead makes it `open` or `filled` again; the cancelled shifts stay cancelled.
- Editing an open or filled opening changes no match. Spots go through the same `open`/`filled` sync as an accept, so lowering them to the accepted count fills the opening and raising them reopens it.
- When a volunteer withdraws from an accepted match, the org's coordinators get an email (`notify.on_withdrawn`). A pending like leaves the inbox quietly.

### (a) Volunteer sign-in by emailed link

```mermaid
sequenceDiagram
    autonumber
    actor V as Volunteer
    participant A as Causematch
    participant DB as SQLite
    participant E as Resend or Gmail

    V->>A: POST /login/send email
    A->>A: ASCII only, at most 254 characters, a bare address, lowercased
    A->>DB: link limits by mailbox, by mailbox and IP, by IP, by IPv6 /48 (and 5 a day per mailbox over Gmail)
    alt over a limit
        A-->>V: 429 Too many tries
    else allowed
        A-->>V: "Check your email", the same page for every address
        Note over A: after the response (notify.after)
        A->>DB: volunteer_link_kind: what does this address get?
        alt a live volunteer holds it (sign-in), or none does (sign-up)
            A->>DB: sign-up only: under the app-wide ceiling, else nothing is sent
            A->>DB: delete this address's earlier tokens, store SHA-256 of a new one, expires in 15 min
            A->>E: email the link /login/verify?token=...
            E-->>V: email
        else a coordinator's or admin's address
            A->>E: their own link, /org/login/verify (see b)
        else a mailbox a suspended account holds, or a staff mailbox with no live staff account behind this exact address
            A->>A: nothing is sent
        end
    end
    V->>A: GET /login/verify?token=...
    A-->>V: token moved into the session, redirect to /login/verify without it
    V->>A: GET /login/verify
    A->>DB: peek: is the token live and unused? It is not used up
    A-->>V: "Sign in as {address}" and a button, with a warning if this browser didn't ask for the link or is signed in as someone else
    V->>A: POST /login/verify
    A->>DB: mark the token used, in one statement
    A->>DB: the address still allowed? Find the volunteer who holds it (or join one), else create one
    alt the profile is suspended
        A-->>V: refusal, no session
    else
        A-->>V: new session uid + version, redirect to /deck or /onboarding
    end
```

Details the diagram doesn't show:

- **Which address gets what** is `data.volunteer_link_kind`, in this order. An exact live staff address that can sign in gets the staff link. A mailbox a suspended account holds gets nothing. A volunteer who already has this exact emailed-link address signs in, even if a `+tag` or dotted form of it has since become a staff mailbox. Then a staff mailbox (`data.staff_mailbox`: any non-deleted coordinator's or admin's address, including one that can't sign in because the coordinator is suspended or has no org, compared by `forms.abuse_key`, so a `+tag` or Gmail's dots don't make it a volunteer's) gets nothing, because one address has one role and may not sign a volunteer up or join one. Last, the volunteer who holds the address signs in (`data.email_sign_in_owner`), and no holder means a sign-up. The same staff-mailbox rule stops a Google or Apple sign-up or join (the callback answers what `POST /login/send` answers for that address, and stores nothing) and Me's Connect. A volunteer who already signed in by that Google or Apple identity keeps signing in. The emailed link has one gap: if a claim is approved, or a coordinator or admin added, for an exact address a volunteer signs in with, the staff check runs first and answers with the staff link, and `POST /login/verify` then refuses the volunteer link, so that volunteer can sign in only by Google or Apple, if one is connected. So each of those asks first. `data.volunteer_holds_address` is true when a live volunteer holds the exact address as an emailed link's identity or a Google or Apple one whose provider hosts the mailbox (never `profile.email`, and a `+tag` is another address). No warning is needed when the address already gets the staff link (it is a coordinator's already). Otherwise the admin's claim card warns, and approval needs its "Yes, make it a coordinator sign-in" box (`data.approve_claim`'s `confirm_volunteer`); without it nothing is approved and the warning shows again. `data.add_coordinator` and `data.add_admin` raise `data.VolunteerAddress` unless given `allow_volunteer` (approval passes its confirm), so every caller gets the check; `scripts/add_coordinator.py` and `scripts/add_admin.py` print the same warning and refuse unless given `--force`. A mailbox a suspended account holds (`data.mailbox_suspended`, so a `+tag` or Gmail's dots count) is refused by all three, confirmed or not, and its claim card has no Approve button. Only an admin sees it: the claim page and its "Request received" answer are the same for every address.
- **One address, one account.** A first sign-in by one way joins the volunteer who already holds its exact lowercased address by another (`data.volunteer_for_identity` and `data.address_holder`), and its `identity` row is attached to that profile: a link to the address of a volunteer's Google or Apple account is a sign-in, not a sign-up, and Google or Apple with the address of an emailed-link account opens that account, so an org's blocks on it cover every way in. Only a proven address joins (`data._proven_address`): verified, ASCII (checked before lowercasing, because Python turns the Kelvin sign into a "k"), and with a non-zero `identity.authoritative`. It is never the profile's typed email, and never by mailbox: `x+1@gmail.com` stays its own account. A holder is a live volunteer, a suspended one included, with an `email` identity whose subject is the address, or another identity with `authoritative = HOLDER` and that address; a Workspace account (JOINER) may join into a holder but never is one (section 9). There is no join, and the sign-in gets an account of its own, when the address is not authoritative (a provider keeps an address "verified" after its mailbox has moved on), when the holder already has an account of the incoming provider (`has_one`: never two of one provider), when the mailbox is suspended (the new profile is born suspended and refused), and for the fake provider on a preview. When two live volunteers already hold one address (made before joining existed), neither is joined, a new profile is made, and a warning logs their profile ids, never the address; either keeps signing in to its own account by its own identity, with the same warning. Every sign-in by an existing identity stores the provider's current email and `authoritative` again, so a renamed account stops holding its old address.
- **The token** is `secrets` randomness stored only as its SHA-256, in `login_token` with the lowercased `email` and no profile. A newer link replaces the older one. A profile is created only when the link is used, as a first Google sign-in creates one (`data.volunteer_for_identity`): no phone, that email, no name. Its `identity` row has `provider = 'email'` and the address as `subject`, so a later link signs in the same account.
- **The link opens nothing by itself.** A mail scanner that fetches it uses up nothing, because only the button's POST consumes the token. The confirm page also guards against login CSRF: the session keeps a keyed digest of the mailbox this browser last asked a link for, for a link's lifetime, and the page warns "This browser didn't ask for this link" when it doesn't match. It sets `Referrer-Policy: same-origin`, because under `no-referrer` a browser sends `Origin: null` with the button's POST and `security.Guard` refuses it.
- **Suspensions follow the mailbox** (`data.mailbox_suspended`): an address that a suspended account holds as a verified address, by the emailed link, Google or Apple, live or deleted with its hash kept, gets no emailed link, and a new profile made by any way in to it is born suspended, so its sign-in is refused. Nothing joins such a mailbox either: a Google or Apple sign-in on it falls through to a new, suspended profile, which is refused. No emailed link is ever sent to it. It stops new links, new profiles and joins only: an existing, unsuspended Google or Apple profile on the same mailbox can still sign in.
- The response is the same whatever the address, and the email goes out after it, so every address takes the same time to answer.

In development and test with `CAUSEMATCH_FAKE_EMAIL=1`, nothing is sent and the link is only logged. A preview with the fake emailer also shows "Open your sign-in link" on the result page, but only for the demo's addresses (`@demo.causematch.test`, `@team.demo.causematch.test`); any other address gets the same page with nothing sent, shown or logged. An admin's link stays in the log.

### (b) Coordinator or admin sign-in by email link

```mermaid
sequenceDiagram
    autonumber
    actor C as Coordinator
    participant A as Causematch
    participant DB as SQLite
    participant R as Resend or Gmail

    C->>A: POST /org/login/send email
    A->>DB: the same link limits as (a), by mailbox, by mailbox and IP, by IP and by IPv6 /48
    A-->>C: "If that address belongs to a coordinator, a sign-in link is on its way.", the same for any address
    Note over A: after the response (notify.after)
    A->>DB: coordinator with an org membership, or admin, by email
    opt known, live account
        A->>DB: delete this user's earlier tokens, store SHA-256 of a new one, expires in 15 min
        A->>R: email the link /org/login/verify?token=...
        R-->>C: email
    end
    C->>A: GET /org/login/verify?token=...
    A-->>C: token moved into the session, redirect to /org/login/verify without it
    C->>A: GET /org/login/verify
    A->>DB: peek at the token, consuming nothing
    A-->>C: "Sign in as {address}" and a button, with the warnings of (a), so mail scanners can't use the link up
    C->>A: POST /org/login/verify
    A->>DB: mark token used, only if unused and unexpired, in one statement
    A-->>C: session set, redirect to /org or /admin
```

In a preview with the fake emailer, submitting a demo coordinator's address on `/org/login` shows that coordinator's link on the result page (never an admin's); any other address gets the unknown-address answer and nothing is sent. The demo coordinator's and admin's links are also in the startup log, each valid once for 15 minutes.

### (c) Google or Apple sign-in

```mermaid
sequenceDiagram
    autonumber
    actor V as Volunteer
    participant A as Causematch
    participant P as Google or Apple
    participant DB as SQLite

    V->>A: GET /auth/{google,apple}/start (?for=org from /org/login)
    A-->>V: 303 to the provider, flow cookie: state, nonce, PKCE verifier (Google), intent, 10 min
    V->>P: consent
    P-->>V: Google: 303 to GET /auth/google/callback?code&state
    Note over P,V: Apple: a form POST from its own origin to /auth/apple/callback,<br/>exempt from the Origin check, so the flow cookie is SameSite=None, Secure
    V->>A: callback with code and state, and the flow cookie
    A->>A: cookie ours, under 10 min, this provider, same state, else 400
    A->>DB: callback rate limit per address, counted only once the state checks out
    A->>P: exchange the code (PKCE verifier, or Apple's ES256 client secret)
    P-->>A: ID token
    A->>A: RS256 signature from the provider's keys, iss, aud, exp, nonce, then email_verified
    alt volunteer
        A->>A: a new sign-up or join on a staff mailbox? Answer as /login/send does for that address and create nothing
        A->>DB: identity (provider, subject) to profile and refresh its stored email, else join the volunteer who holds the exact proven address, else a new volunteer with no phone, re-applying retained blocks and suspension, also those kept for the verified mailbox
        A-->>V: session, redirect to /deck or /onboarding
    else coordinator or admin (?for=org)
        A->>A: does the provider host this mailbox? Gmail, a matching Workspace domain, or iCloud
        A->>DB: staff_by_email(verified email), creating nothing
        A-->>V: session and /org or /admin, or the org login page with a link to /org/claim, or the emailed link's answer when the provider doesn't host the mailbox
        Note over A,V: on a preview, a staff sign-in is limited to a demo coordinator, never an admin or an address off the demo's domains
    else connect from Me
        A->>DB: link to the sealed uid while its session version is unchanged
        A-->>V: redirect to /me/profile with a notice or the reason it was refused
    end
    Note over A: the flow cookie is deleted whatever the outcome
```

With `CAUSEMATCH_FAKE_OAUTH` (development, test, preview) the start redirects to a local consent page, `/auth/fake/consent`, which mints a one-time code bound to the nonce (and the PKCE challenge, for Google) and sends it through the same callback. `1` stands in for Google alone, so a preview shows no Apple button it was never configured for; a list such as `google,apple` (the tests) names each. On a preview nobody types the account: the start route seals a random subject in a cookie (`__Host-cm_fake_sub`), so each browser comes back to its own account and no other browser can reach it, and "Use a different account" draws a new one. On a preview, the consent page checks the posted flow is the one in the browser's own flow cookie (`_own_flow`), so a link carrying someone else's flow can't mint a code for this browser's account. On a preview the fake Google signs in a volunteer under the browser's own random subject, with whatever email is typed. Because it proves no address, its identity is untrusted: it joins no account and none joins it (it is stored with `authoritative` 0), and the staff-mailbox rule checks only demo addresses, so every other address gets the same answer, the browser's own account, whether it is a staff address or not. Only the sign-in callback gives every non-demo address that same answer: Me's Connect still checks `staff_mailbox` for the fake too. A staff sign-in is limited to a demo coordinator, never an admin or an address off the demo's domains. `docs/OPERATIONS.md` (Security, Sign-in) has the details and the reasons.

### (d) Swipe, like, accept, shift, text, chat

```mermaid
sequenceDiagram
    autonumber
    actor V as Volunteer
    participant A as Causematch
    participant DB as SQLite
    participant R as Resend or Gmail
    participant T as Email, or Twilio SMS
    actor C as Coordinator

    V->>A: POST /swipe opening_id, liked=1, via htmx
    A->>A: swipe token bucket
    A->>DB: check the opening is in this volunteer's deck
    A->>DB: insert swipe, insert match pending, org_blind if the org blocked them
    A-->>V: next card, swapped into the deck area
    opt match is visible to the org, and under 20 like notices from this volunteer in 24 h
        Note over A,R: after the response (notify.after)
        A->>R: "New volunteer interest", at most once per org per 15 min
        R-->>C: email
    end
    C->>A: GET /org/inbox
    A-->>C: shared fields only, "Sam L."
    C->>A: POST /org/matches/{id}/decide accept
    A->>DB: one UPDATE: pending to accepted only while accepted count is under slots
    A->>DB: opening open/filled sync, insert shift kind=accept at the opening's start if still ahead, with the opening's length
    Note over A,DB: a decline is the same UPDATE with undo_until set two minutes ahead, and POST /org/matches/{id}/undo puts it back to pending
    Note over A,T: after the response (notify.after)
    A->>T: "Org said yes to Opening", once per match: an email, or a text under the SMS ceiling when the volunteer added a number
    T-->>V: "Org said yes to Opening! Say hi:" with a link to /chat/{id}
    V->>A: GET /chat/{id}, websocket /chat/{id}/ws
    C->>A: GET /org/chat/{id}, websocket /org/chat/{id}/ws
    C->>A: ws frame msg
    A->>DB: save message
    A-->>V: pushed over every open socket on the match
    Note over A,T: beside the socket, which takes its next frame at once (notify.after)
    opt volunteer has no socket open on this match
        A->>T: "New message from Org", email or text, at most once per match per 30 min
    end
```

If the accepted match came from a like on an urgent call, the shift is booked at the call's time instead of the opening's. Either way the volunteer's Matches number goes up, and an open deck or Matches page shows the "It's a match!" takeover within about 10 seconds (`GET /deck/news`). If the accept notice fails, or a text is deferred by the SMS ceiling, the reminders task (`POST /tasks/reminders`, planned hourly, not set up) retries it, up to 5 attempts in all. A deferred try doesn't count as an attempt.

### (e) Urgent call: post, notification waves, auto-accept

```mermaid
sequenceDiagram
    autonumber
    actor C as Coordinator
    participant A as Causematch
    participant DB as SQLite
    participant S as Cloud Scheduler (planned, not set up)
    participant T as Email, or Twilio SMS
    actor V as Volunteer

    C->>A: POST /org/openings/{id}/urgent starts_at, needed, auto_accept
    A->>DB: check: opening open, no open call, start within 48 h, needed within open spots, at most 3 calls per org per 24 h
    A->>DB: insert urgent_call status=open
    Note over S,A: Nothing calls this today, and in the preview the password gate blocks it.
    loop each POST /tasks/urgent (planned every 10 min)
        S->>A: POST /tasks/urgent, Bearer TASKS_TOKEN
        A->>DB: close stale calls, find calls whose wave is due
        Note over A,DB: wave 1 at the first run, wave 2 thirty minutes later if not filled. Size: 3 x people still needed, at most 25
        A->>DB: pick recipients, best fit first
        Note over A,DB: opted in, the card is in their deck, within travel time, free then,<br/>outside quiet hours, within their window, under the weekly cap of at most 3,<br/>not already told, no shift within 2 h of it, not blocked by the org
        alt nobody eligible right now
            A->>A: wave not spent, retried next run
        else someone is eligible
            A->>DB: claim the wave, once only
            A->>T: SMS if they added a number that never replied STOP, else email with an unsubscribe link if they have an address, else skipped
            Note over A,T: SMS counts against the global ceiling and the urgent half of it, otherwise recorded deferred
            T-->>V: link to /deck?card={opening}
        end
    end
    V->>A: GET /deck?card={opening}
    A-->>V: urgent cards rank first, the linked card on top
    V->>A: POST /swipe liked=1, call_id
    A->>DB: insert match with urgent_call_id
    opt auto_accept and fewer than needed accepted and slots left
        A->>DB: one UPDATE to accepted, insert shift at the call's time, call filled once needed are in
        A->>T: email the org's coordinators "accepted automatically"
        A-->>V: HX-Redirect to /chat/{id}
    end
```

Edge cases:

- If a volunteer likes a card drawn before a call was posted on that opening, nothing is recorded and they are shown the call's card first.
- A like that arrives just after the call filled still counts toward it, but stays pending for the org.
- An urgent email's unsubscribe link `/urgent/off/{volunteer id}-{HMAC}` works signed out. `GET` only shows a page with a button; the `POST` is what turns calls off, so a mail scanner that fetches the link changes nothing. The email also carries a `List-Unsubscribe` header.

### (f) Day-before reminders and retries

```mermaid
sequenceDiagram
    autonumber
    participant S as Cloud Scheduler (planned, not set up)
    participant A as Causematch
    participant DB as SQLite
    participant T as Email, or Twilio SMS
    actor V as Volunteer

    S->>A: POST /tasks/reminders, Bearer TASKS_TOKEN (planned hourly job, not set up)
    A->>A: constant-time token compare, else 401
    A->>DB: "you're matched" texts that failed or were deferred, with tries left
    A->>T: resend each
    A->>DB: new-message texts the SMS ceiling deferred
    A->>T: resend each
    A->>DB: scheduled shifts of accepted matches starting 23 to 25 h from now
    loop at most one per volunteer and 100 per run
        A->>DB: claim notification reminder:{shift}
        A->>T: "Reminder: Opening with Org is on Sat Oct 18, 9:00 AM. Still coming? PUBLIC_BASE_URL/shifts/{id}/confirm"
        T-->>V: an email, or an SMS when the volunteer added a number
    end
    A->>DB: blank name, email and phone of claims decided more than 90 days ago
    A-->>S: "sent N"
```

Once an hourly job calls it, the two-hour window puts each shift in at least one run, and the notification key keeps it to one send. No job calls it yet. A shift added less than 23 h before it starts never gets a reminder. The link in the text carries no sign-in token: a signed-out volunteer signs in first. "Yes, I'll be there" stamps `shift.confirmed_at` once, and "Can't make it" leads to the cancel page in (g).

### (g) Can't make this one, and a late cancel's refill

```mermaid
sequenceDiagram
    autonumber
    actor V as Volunteer
    participant A as Causematch
    participant DB as SQLite
    participant R as Resend or Gmail
    actor C as Coordinator

    V->>A: GET /shifts/{id}/cancel, from Matches, the chat or the Still coming? page
    A->>DB: the volunteer's own upcoming shift on an accepted match, else 404
    A-->>V: confirm page, says whether this gives up the spot
    V->>A: POST /shifts/{id}/cancel, optional reason (300 chars max, never stored)
    A->>DB: shift cancelled, cancelled_by = volunteer
    alt one-time opening, open or filled, and this was its only upcoming shift
        A->>DB: match withdrawn, opening back to open if a slot freed
        opt starts within 48 h, no refill posted for this opening and time before
            A->>DB: urgent_call for 1 person at the shift's time, refill = 1
            Note over A,DB: same rules as a coordinator's call: an open spot, no other open call, 3 a day.<br/>Posted for the coordinator who accepted. Auto-accepts if the org's last call did
        end
    else any other case: recurring, flexible, a closed opening, or other shifts left
        A->>DB: the match stays accepted
    end
    A->>R: email the org's coordinators, once per shift, no volunteer name
    R-->>C: reason, whether a call went out or could be posted, chat link
    A-->>V: redirect to /matches with a notice
```

The reason is never stored: `data.cancel_my_shift` has no column for it, and it goes only into the org's email. A refill is posted once per opening and start time, so a refill's own volunteer cancelling again can't send more texts. Its waves then follow the same rules as any urgent call (section 10e).

### (h) Marking a shift and confirming hours

The rules live in `data.complete_shift`:

- Only a member of the shift's org can mark it, and only once it has started. A cancelled shift can't be marked.
- **Attended** stores `status = completed` and `confirmed_minutes`: the minutes the coordinator picked (one of `choices.SHIFT_HOURS`, 30 minutes to 12 hours in 30-minute steps), or the shift's own `duration_minutes` when they left the choice alone. The page requires hours, so a shift with no length (made before migration `0014`) asks for them.
- Hours above both the shift's length and the time since it started are refused (`HoursRefused`), so nobody confirms 12 hours of a 1-hour shift at its start.
- **No-show** stores `status = no_show` and no hours.
- The first mark stamps `marked_at` and `marked_by`. Marking the same result and hours again changes nothing. A different result, or different hours on an attended shift, is the one correction, allowed within 7 days of the first mark; it stamps `corrected_at` and `corrected_by`.
- `data.my_hours` builds the volunteer's hours: completed shifts with confirmed minutes, grouped by month (newest first) with a total each, and named by the coordinator who last confirmed them (`corrected_by`, else `marked_by`), else the org. Completed shifts with no confirmed minutes are listed apart. A block hides nothing here: it is the volunteer's own record.
- Metrics (`app/metrics.py`) count attended shifts and no-shows, not hours.

### (i) A certificate: issuing it, the PDF, and checking it

```mermaid
sequenceDiagram
    autonumber
    actor V as Volunteer
    participant A as Causematch
    participant DB as SQLite
    actor X as Certificate checker

    V->>A: POST /hours/orgs/{org_id}/certificate period (a month, or all)
    A->>DB: the volunteer's attended shifts at that org with confirmed hours, in the period
    alt none there, a bad period, no last name, or hours confirmed before the last rename
        A-->>V: 404 when there are no hours there, else back to /history with a notice
    else the same content is already issued and not withdrawn
        A-->>V: redirect to that certificate
    else 20 already issued in 24 h
        A-->>V: back to /history with a notice
    else
        A->>DB: withdraw and scrub the earlier one for this org and period
        A->>DB: insert id (16 random bytes), snapshot JSON, HMAC-SHA256 of id and snapshot
        A-->>V: redirect to /hours/certificate/{id}
    end
    V->>A: GET /hours/certificate/{id}.pdf
    A->>DB: the owner's, not withdrawn, signature still matches, else 404
    A->>A: pdf token bucket, 10 then 1 a minute
    A-->>V: PDF from the snapshot: signature code, QR code of PUBLIC_BASE_URL/check/{id}
    V-->>X: gives the PDF, the printout or the link
    X->>A: GET /check/{id}, no account
    A->>DB: rate limit per address, 30 per 10 min, else 429
    A->>DB: the certificate by id
    alt no such id
        A-->>X: 404 "Not a Causematch record"
    else withdrawn
        A-->>X: "This certificate was withdrawn." and nothing else
    else signature recomputed from the snapshot with CERTIFICATE_KEY does not match
        A-->>X: the same 404 "Not a Causematch record"
    else genuine
        A->>DB: is each listed shift still attended with the same confirmed minutes?
        A-->>X: "Genuine certificate" or "Hours have changed": name, nonprofit, EIN, period, total, issue date, signature code
    end
```

The rules live in `data.issue_certificate` and `data.check_certificate`:

- A certificate covers one volunteer, one nonprofit and one period, a month (`2026-10`) or `all`. It lists only completed shifts with confirmed minutes, by the shift's start, oldest first, read as `data.my_hours` reads them (the coordinator who last confirmed each, else the org). Like `my_hours`, it doesn't look at blocks or deactivation: it is the volunteer's own record.
- Shifts and hours lists a card only for a nonprofit with confirmed hours (`data.certificate_options`), and offers its months and "All my hours here".
- **Refusals.** No last name (`NameRequired`): a certificate carries the full name. Hours confirmed (`coalesce(corrected_at, marked_at)`) at or before `profile.name_changed_at`, or with no time recorded, once the name has changed (`NameChanged`). Then, only for new content, `CERTIFICATES_PER_DAY` (20) already issued in the last 24 hours, withdrawn ones included (`CertificateLimit`).
- **Issuing twice.** If an active certificate for the same nonprofit and period still verifies and says the same thing apart from the issue date, that one comes back and nothing is written, even at the daily limit. Otherwise the earlier one is withdrawn and scrubbed, in the same transaction as the new insert, so each nonprofit and period has at most one live check link.
- **The PDF** is built on request from the stored snapshot, never stored, in a thread (about 0.1 to 0.5 s of CPU each, hence its own token bucket). It is served `private, no-store` as an attachment named `causematch-hours-{nonprofit}-{period}.pdf`. The certificate page and the PDF both refuse (404) a certificate whose signature no longer matches.
- **Withdrawing** (`data.withdraw_certificate`) stamps `withdrawn_at` and erases the snapshot and signature at once. Withdrawing again changes nothing. A new certificate can be issued from Shifts and hours.
- **"Hours have changed"** means a coordinator corrected a shift that is on the certificate after it was issued: it is no longer attended, or its confirmed minutes differ, compared shift by shift, so two corrections that cancel out in the total still count. Shifts worked later are not on it and change nothing. The figures shown stay the ones issued.
- In the preview the check link sits behind the password gate like every other page, so a checker without the preview password gets the gate's 401.

---

## 11. Matching and ranking

The deck is built in `app/data.py` from the volunteer's preferences (`app/prefs.py`), and scored by pure functions in `app/match.py`.

```mermaid
%%{init: {"flowchart": {"curve": "basis"}, "themeVariables": {"fontFamily": "Helvetica, Arial, sans-serif"}}}%%
flowchart LR
    all["1. Open openings<br/>starting in the future"] --> f1["2. Hard filters"] --> f2["3. My Only show<br/>choices"] --> f3["4. Session<br/>filter sheet"] --> rank["5. Rank"] --> cards["6. Cards"]

    classDef step fill:#85bbf0,stroke:#5d82a8,color:#0b1f33
    classDef key fill:#438dd5,stroke:#2e6295,color:#ffffff
    class all,f1,f2,f3,rank step
    class cards key
```

1. **Open openings** whose start is still ahead (an opening with an open urgent call is judged at the call's time).
2. **Hard filters**, always on: a verified, active org the volunteer hasn't blocked; at least one active cause; not swiped yet; no cause marked "not for me"; a separate orientation only if the volunteer said that's fine; within the volunteer's horizon (how far ahead they look), or 48 hours in today mode ("Next 48h").
3. **Only show choices** (dealbreakers), each only when picked:
   - distance: within some place's travel time (on by default);
   - causes: only loved or open ones;
   - when: free then, or a flexible opening, and one-time/recurring fits (not applied in today mode);
   - work: no contradicting setting, effort or group size;
   - "I need my hours confirmed" ticked (`verified_hours = "only"`): only orgs that confirm hours. It is a checkbox of its own rather than a dealbreaker toggle, and it applies in today mode too.
4. **The session filter sheet**: the main place's travel time (only while distance is a dealbreaker; it can widen or narrow it), hidden causes, this week only.
5. **Rank**: a week already at the weekly limit last, then urgent calls first, then fit score, then soonest. Today mode ranks soonest, then nearest.
6. **Cards**: the linked card (from an urgent call's text) on top. With "Surprise me", about 1 in 8 comes from outside the loved causes (it needs a loved cause, is off in today mode, and never replaces an urgent top card). Each card has 1 to 4 why-chips and the travel minutes.

**Places and travel time.** A volunteer helps from at most 2 places. A place's point is either a ZIP or neighborhood centroid, or a device location rounded to 2 decimals (about 1 km) and checked to fall within a box around LA County. There's no routing API: travel minutes are the straight-line (haversine) distance divided by a door-to-door speed for the mode (car 32 km/h, transit 14, bike 13, walk 4.5). A place's reach is `max_minutes × speed`, and 0 means any distance. Each card is measured from whichever place gives the quickest trip. Remote openings skip distance entirely.

**Fit score** (`app/match.py`) is a weighted sum where each term is between 0 and 1:

| Term | Weight | How it's computed |
|---|---|---|
| Causes | 0.30 | Each of the opening's causes counts 1.0 if loved and 0.6 if open, divided by the size of the union of its causes and the volunteer's loved causes. |
| Distance | 0.18 | 1 at the place, falling linearly to 0 at the place's reach (60 km for "any"). Remote openings score 1. |
| Availability | 0.15 | 1 if the start falls in a ticked day slot or on a free date, 0.5 for a flexible opening, else 0. Halved if one-time/recurring doesn't fit. |
| Skills | 0.12 | The share of the opening's skills the volunteer has. 0.5 when the opening asks for none. |
| Work | 0.10 | Mean of four terms. Setting, effort and group size each score 1 if they agree, 0 if they differ, 0.5 if either side doesn't say. First-timer scores 1 when the volunteer asked for it and the opening is, else 0.5. |
| Reply rate | 0.10 | The org's share of eligible matches decided within 48 h. 0.5 with no history. |
| Language | 0.05 | 1 if the volunteer speaks a language the opening says helps, else 0.5. A language is never required. |

Whether an org confirms hours never changes the score: it only hides other orgs' openings when "I need my hours confirmed" is ticked, and adds the "Confirms your hours" chip. A volunteer who speaks a language besides English counts as having the "language" skill when an opening asks for it.

**Weekly limit.** "Up to N shifts a week" (off, stored as 0 or NULL, or 1 to 5, `app/prefs.py` `weekly_limit`) counts the volunteer's shifts that aren't cancelled in each Los Angeles week, Monday to Sunday. In a week that is already full, the deck ranks that week's openings after every other week's and marks them "Your week is full", and urgent waves skip the volunteer for calls in that week.

**Why-chips** explain each card, most telling first: up to two cause chips (♥ for loved), then "Surprise pick", "Remote", "You're free that day" or "You're free Sat mornings", first-timer friendly (here only if the volunteer asked for it), matching kinds of work, "Spanish helps"-style language chips, and last, first-timer friendly when the volunteer didn't ask for it. Normal cards show up to 4 chips. Urgent cards show 2, because their badge ("Last-minute · tomorrow 9 am · needs 2") and an optional "Like and you're booked" line take the room. Three chips keep their place by pushing out the last ordinary reasons, so a card still shows at most 4 (2 when urgent): "You're booked at {org} then" when the card starts less than 4 hours (`CLASH_HOURS`) from a shift the volunteer already has, "Confirms your hours" for an org that does (the one that gives way when they don't all fit), and "Your week is full". An urgent card with a clash and a full week shows only those two.

**Empty deck.** Empty views are logged into `deck_empty` for the metrics, at most once per volunteer per hour, with today mode counted separately. The empty state offers the single change that would add the most cards: widen a place's travel time, look further ahead, drop one dealbreaker, show nonprofits that don't confirm hours too (when "I need my hours confirmed" is ticked; it unticks it), or clear the filter sheet.

The same visibility check guards `POST /swipe`, so a like sent by id for an opening the volunteer couldn't see is refused. The same fit score orders who gets an urgent call's notices.

---

## 12. Security and privacy

### Who sees what, and when

| Data | Who sees it |
|---|---|
| Volunteer's phone, email, places, coordinates, connected Google or Apple accounts | Nobody else. Coordinates are never rendered, and cards show only the travel minutes from the nearest place. (`data._distance_label` computes whole miles, but the card shows them only when there is no travel time, and with a place both are set, so in practice miles never appear: `app/routes/deck.py`, `place_line`.) |
| Match preferences (cause ratings, dealbreakers, free dates, weekly limit, the verified-hours choice, last-minute settings) | Only the volunteer. Nothing records why a volunteer needs their hours confirmed. |
| What the volunteer shares (`SHARED_WITH_NONPROFIT` in `app/choices.py`): photo, first name and last initial, pronouns, bio, interests, causes (A to Z, without saying which are loved), general availability (weekday/weekend × part of day), languages, frequency, neighborhood | A nonprofit, only after the volunteer likes one of its openings. The inbox query selects only these columns. |
| Full last name | The nonprofit, once it accepts. |
| A shift's confirmed hours and "Still coming?" answer | The volunteer, and the coordinators of the shift's org. |
| A certificate as issued: full name, the nonprofit's name, EIN and website, each shift's date, role and hours, the coordinators who confirmed them, the total, the period, the issue date and the signature code | The volunteer, on its page and in its PDF, and whoever they give the page, printout or PDF to. No coordinator or admin page shows certificates. |
| What a certificate's check link shows | Anyone holding the link or the QR code, signed in or not: the full name, the nonprofit and its EIN, the period, the total, the issue date, the signature code, and whether the nonprofit has corrected those hours since. Never the shifts, the website, the coordinators, a phone or an email. A withdrawn certificate's link shows only that it was withdrawn. |
| The reason given when cancelling a shift | Never stored. It goes only into the email to the org's coordinators, which doesn't name the volunteer. |
| Whether an org confirms volunteer hours | Volunteers, as the "Confirms your hours" chip on its cards. Only the org's members can change it. |
| A volunteer's photo (`/photos/{key}.jpg`, `/photos/thumb/{key}.jpg`) | The volunteer, admins, and coordinators of an org whose inbox shows one of their matches, with no block either way. Anyone else gets 404, the same as for a missing photo. It's always revalidated (`private, no-cache`), so a block or removal hides it at once. |
| Org logo and photos, opening photos | Anyone signed in. These may be cached for a day. |
| After a block, either way | The volunteer's waiting and decided rows leave the org's inbox. Past matches and shifts stay, labelled only "Volunteer", with no photo or details. A like from a volunteer the org blocked looks normal to the volunteer but never reaches the org (`org_blind`). |
| Pilot metrics | Admins only. Aggregates, small groups merged, no names, phones or emails. |

### Photo pipeline (`app/photos.py`, `app/photo_worker.py`)

```mermaid
flowchart LR
    up["Upload POST, up to 6 MB<br/>photo.js shrinks it in the browser to 1600 px first, logos excepted"] --> bucket["upload bucket<br/>10, then 1 a minute"]
    bucket --> check["Header checks in the app<br/>format JPEG, PNG, WebP, GIF<br/>pixel caps: JPEG 50 MP, WebP 8 MP, PNG/GIF 12 MP<br/>JPEG marker walk: max 50 scans, coefficient budget"]
    check --> slot["one of 2 decode slots<br/>wait up to 5 s, else Busy"]
    slot --> child["python -I app/photo_worker.py<br/>env PATH and LANG only, no secrets<br/>no core dumps, RLIMIT_CPU 8 s<br/>Linux: RLIMIT_AS 512 MB<br/>killed after 10 s or 4 MB output"]
    child --> out["fresh JPEGs, no EXIF or GPS<br/>full + 160 px thumb"]
    out --> store[("photo table<br/>random 22-char key")]
```

The child process runs as the same user as the app: it limits resources but doesn't drop privileges. macOS doesn't enforce `RLIMIT_AS`, so in development only the CPU limit and the timeout apply. CI checks that the memory cap really runs on Linux.

### Request-level defences (`app/security.py`)

- **Same-origin writes.** Every unsafe method needs `Origin` equal to `PUBLIC_BASE_URL`'s origin or, when the browser sends no `Origin`, a `Referer` on it. The only exemptions are `/tasks/reminders`, `/tasks/urgent` and `/auth/apple/callback`, which Apple posts from its own origin and which acts only with a flow cookie whose state matches (10c). Websockets need the exact `Origin`.
- **Headers on every response:**
  - A CSP of `default-src 'self'`, `script-src 'self'`, `style-src 'self'` with no inline code, `img-src 'self' data: blob:`, `connect-src 'self'` plus the ws origin, `font-src 'self'`, `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'` plus `https://accounts.google.com` and `https://appleid.apple.com` (Me's Connect form answers with a redirect to them) and `object-src 'none'`.
  - `X-Frame-Options: DENY`, `nosniff`, `strict-origin-when-cross-origin`.
  - A permissions policy that allows geolocation only on `/onboarding`, `/me/preferences` and `/me/preferences/causes`.
  - `Cache-Control: no-store`, except on `/static` and `/favicon.ico`, unless the route sets its own (photos do).
  - HSTS in production and preview.
  - `X-Robots-Tag: noindex, nofollow` in preview.
- **Body limits:** 64 KB, or 6 MB on photo upload paths. Over that, 413.
- **Errors in production:** a plain 500 page with a reference id. The log line keeps the request id and stack frames but not the exception message, which could hold a phone number. The uvicorn access log redacts `token`, `access` and `code` in query strings (`security.REDACTED`), and the logs mask phones and emails. A certificate's id in a path (`/check/{id}`, `/hours/certificate/{id}` and its `.pdf` and `/withdraw`) is replaced by `[redacted]` too, in the access log and in that error line (`security.redact_query`), because the id is the check link's only credential. Cloud Run's own request log still records full URLs.
- **Client IP:** taken from `X-Forwarded-For`, exactly `TRUSTED_PROXY_HOPS` places from the right. IPv6 clients are counted per /64.

Rate limits fall into two kinds:

| Kind | Limits |
|---|---|
| Persistent, stored in `rate_event` so a restart doesn't reset them | **Emailed sign-in links** (`/login/send` and `/org/login/send` share one budget, `security.link_limits`, counted by mailbox as `forms.abuse_key` reads it, so a `+tag` or Gmail's dots change nothing): 5 per hour per (mailbox, client address) and 20 per day per mailbox; 20 per hour per client address (an IPv6 /64) and 40 per hour per IPv6 /48; and, while email goes through Gmail, 5 per day per mailbox. Every address of a valid shape is counted, so a refusal says nothing about the account. **Sign-up links, app-wide** (`SIGNUP_ALL`): 100 per hour and 500 per day for all of them together, checked when sending, after the page has answered, so it changes nothing a visitor sees; sign-in and staff links don't count, so a flood of sign-ups can't lock existing accounts out. **Add-a-number codes** (Me, only with `CAUSEMATCH_COLLECT_PHONE=1` and Twilio Verify or the fake): 5 per 10 min and 10 per day per (phone, IP); 20 per day per phone; 60 per hour per IP; 120 per hour per IPv6 /48. **Wrong codes:** 10 per 10 min per phone. **Like notices to nonprofits:** 20 per 24 hours per volunteer (`LIKE_NOTICES`); later likes show only in the org's inbox. **Google and Apple callbacks:** 30 per 10 minutes per IP, counted once the flow cookie and state check out. **Certificate check links:** 30 per 10 minutes per IP (`CHECK_PER_IP`). **Demo sign-ins** (a demo only): 10 demo volunteers per hour per IP, also counted against the IP's `LINK_PER_IP`; the one-tap nonprofit and admin demos count against `LINK_PER_IP` too. **Claims:** 5 per hour per IP, 3 per day per email. **ProPublica calls:** 30 per hour across all admins; the claim page's name search has its own, 60 per hour per IP and 300 per hour for every visitor together. **SMS ceiling** on every text: `SMS_MAX_PER_HOUR` (default 100) and `SMS_MAX_PER_DAY` (default 500). Urgent waves may use only half of each, so add-a-number codes and other texts always have room. **Email ceiling** over SMTP: see below. |
| In-memory token buckets per user | Swipe: 30, then 1 per second. Chat: 20, then 2 per second. Report/block: 10, then 1 every 6 minutes. Saving an opening's edit: 10, then 1 per minute (each save can write a line into several chats outside the chat bucket). Uploads: 10, then 1 per minute. Certificate PDFs: 10, then 1 per minute (taken in the route, not by `throttle`). Wrong preview keys, cookies and passwords: 10 per client address, then 1 per minute. |

HTTP requests over a limit get a 429 page with `Retry-After` (600 s for the persistent limits). Chat frames over the bucket close the socket with 1013. Texts over the SMS ceiling are deferred, and emails over the SMTP ceiling fail (below). A reporter may also file at most 10 reports in any 24 hours, counted from the `report` table; the 11th is refused on the form.

### Emailed sign-in links (`app/routes/auth.py`, `app/data.py`)

- **One answer for every address.** `/login/send` and `/org/login/send` answer the same page whether or not an account holds the address, and send after the response, so neither the words nor the time give an account away. The only refusals are a malformed address and a 429 for too many tries, and both come before the address is looked up.
- **The token** is 32 random bytes, stored as its SHA-256, single-use, valid 15 minutes. A volunteer's is tied to the lowercased address, not to a profile (`login_token.email`, with `user_id` NULL), so no profile exists until the link is used. A newer link for the same address or profile replaces the older one.
- **The link doesn't sign anyone in.** A GET moves the token into the session and redirects without it, so the page and its assets' `Referer` never carry it; the confirm page only peeks at it; the button's POST uses it up in one conditional statement and checks again that the address may still sign in. A mail scanner that fetches the link therefore uses up nothing. Between the GET and the POST the raw token sits in the session cookie, which is signed but not encrypted. The verify POSTs also accept a `token` form field when the session has none; `security.Guard`'s same-origin check protects those POSTs like any other.
- **Login CSRF.** Pressing the button on a link someone else sent would sign this browser in to their account. The confirm page names the account ("Sign in as {address}") and warns when this browser didn't ask for the link (the session holds a keyed digest of the mailbox it last asked for, for 15 minutes; the address itself is never kept in the signed cookie) and when it is signed in as another account.
- **Suspended mailboxes.** `data.mailbox_suspended` is true for a mailbox that a suspended account holds as a verified address by any way in, live or deleted with its hash kept. For such a mailbox no emailed link is sent, a new Google or Apple profile is created with `suspended_at` set (born suspended) and its sign-in is refused, and a Connect is refused. Nothing joins such a mailbox. An existing, unsuspended Google or Apple profile on the same mailbox can still sign in. A profile's typed email doesn't count: it is unverified, and counting it would let anyone block someone else's address.
- **One address, one role.** A coordinator's or admin's mailbox (`data.staff_mailbox`, by `forms.abuse_key`) signs no volunteer up and joins none, by the emailed link, Google, Apple or Connect. It applies to new sign-ups and joins only, so a volunteer who signed in by that Google or Apple identity, or by an emailed link to a `+tag` or dotted form of it, before keeps signing in. A volunteer's exact emailed-link address that is later approved as a coordinator's or admin's gets the staff link instead, as 10a says; claim approval and the add-coordinator and add-admin scripts warn before that happens.
- **One address, one volunteer account.** Joining uses only an address the sign-in proves: the emailed link's, or a provider's verified email where the provider hosts the mailbox (`oauth.authoritative`). It is the exact address, never `forms.abuse_key`, so a `+tag` can't be used to reach someone's account, and never `profile.email`, which anyone can type in Me. The gap this closes: with a provider's "verified" alone, a recycled work mailbox could open its former owner's account. Section 10a has the rules.
- `docs/OPERATIONS.md` (Sign-in) has the details and the reasons.

### Email over Gmail (`app/notify.py`)

For the beta, every email can go through one Gmail account over SMTP instead of Resend (`EMAIL_API_KEY` set means Resend is used). `SMTPEmailer` always uses TLS with the certificate checked (STARTTLS on 587, TLS from the start on 465) and refuses a server that offers no STARTTLS, so the app password never crosses in the clear. Each send has an overall deadline of 30 seconds: one that runs out after the message may have reached the server is recorded `unknown` and never retried, one that runs out before is `failed` and retried. After a deadline or a failed connect it doesn't connect again for 60 seconds, and for 10 minutes after Gmail refuses the password.

A free Gmail account may send to about 500 recipients a day, and Google may lock one that goes over. So every email counts against `SMTP_MAX_PER_DAY` in any 24 hours (default 400, at most 450 through `smtp.gmail.com`), and each kind against a share of it, with the staff's sign-in links keeping the last 10%:

| Kind | May use |
|---|---|
| everything but staff's sign-in links | 90% |
| notices, within that | 50% |
| last-minute calls' emails, among the notices | 25% |
| sign-up links, within the 90% | 25% |
| staff's sign-in links | the whole: the last 10% is theirs alone |

An email the ceiling refuses raises `EmailCeiling`: a notice is recorded `failed`, a sign-in link is simply not sent. `RunAfter` runs notices, sign-in links and the chat's new-message notice in worker threads of their own (at most 4 at once, apart from the shared pool), so a slow Gmail holds at most those 4 threads and never a page or a chat socket's frames. A websocket's notices run in a task group of the socket's own, so the socket's end waits for them as a response's end does; when the server stops first, those still waiting are cancelled and logged (`up to N notices not sent: the server is stopping`), as an HTTP request's are, and one already in its thread finishes. Claim approval and rejection emails are the one exception: they go out inline in the admin's request, whose page says whether they went. Also, `notify.batch()` lets the sends of one thread share one signed-in connection. The ceilings are counted in this instance's SQLite file, and the pause after a failed connect (`SMTP_BREAK`) is kept in the process's memory, which is why Gmail needs one instance. The privacy page names the provider, and says Gmail's copies go after a day only when `GMAIL_PURGE=daily` says the purge is installed. `docs/OPERATIONS.md`, Email through Gmail, has the setup, the alerts and the move back to Resend.

### Preview gate

When `CAUSEMATCH_ENV=preview` and `PREVIEW_PASSWORD` has at least 16 characters, `PreviewGate` lets a request through only with the access cookie or the password by HTTP Basic auth, on every request: pages, `/static`, `/photos`, `/tasks` and websocket handshakes, so in a preview nothing can call `/tasks/*` (see section 13). The link people are sent carries the password as `?access=`. On a top-level navigation (`Sec-Fetch-Dest: document` with `Sec-Fetch-Mode: navigate`, or no Sec-Fetch headers, as from curl) a GET with the right key gets a 303 to the same URL without the key and the cookie `__Host-cm_preview` (Secure, HttpOnly, SameSite=Lax, 30 days). Any other request with `?access=` is refused without weighing the key, so a foreign page's `<img>` can't use up a visitor's guesses; a browser that has the cookie just gets the 303 without the key. The cookie is `<issue time>.<mac>`, the mac an HMAC-SHA256 keyed with `SESSION_SECRET` over a label, the password's SHA-256 and the issue time: it can't be made without the secret, expires after 30 days on the server side too, and a new password (or session secret) voids every one. Basic auth (any user name) stays for scripts. The password is compared in constant time on SHA-256 digests. Anything else gets a plain 401 page with no `WWW-Authenticate`, so browsers show no dialog; websockets are closed with 1008. Wrong keys, cookies and passwords share the per-address limit above. Only `/robots.txt` answers without it, with `Disallow: /`.

**A preview is for demo data.** Behind the password, a preview signs in no real coordinator or admin, and no real address gets an emailed link, and its banner says "demo data, wiped at any time · no real details" (`app/config.py`, `demo_address` and `demo_number`):

- The emailed link, volunteers' and staff's, goes only to the demo's addresses (`@demo.causematch.test` and `@team.demo.causematch.test`). Any other address gets an unknown address's answer, with nothing sent, shown or logged. With the fake emailer the result page shows the link for the demo's addresses, an admin's excepted.
- With `CAUSEMATCH_DEMO_MATCHES=1`, anyone holding the preview password can also act as the demo admin ("Try it as an admin", a normal admin session) on demo data only. What they change lasts until the instance is replaced, so one visitor's suspension or deactivation shows for everyone until then. The fake Google still never signs in an admin, and the admin's emailed link stays in the log.
- The fake Google account is the browser's own random subject, sealed in a cookie, and a volunteer signing in with it can type any email. Only a staff sign-in is limited: it signs in a demo coordinator only, never an admin or an address off the demo's domains.
- Me's add-a-number, where `CAUSEMATCH_COLLECT_PHONE=1` offers it, sends its fake code to the fictional numbers 555-0100 to 555-0199 only.
- The database is on the instance's disk and wiped whenever the instance restarts or is redeployed.

### Certificates and the check link (`app/signing.py`, `app/data.py`)

- **What is signed.** At issue, `signing.sign` computes HMAC-SHA256 over a canonical encoding of `{"id": <certificate id>, "snapshot": <snapshot>}`: JSON with sorted keys, no spaces, ASCII only, so the same content always gives the same bytes. The key is `CERTIFICATE_KEY` (`config.certificate_key`), a secret of its own: not the session secret and not `PHONE_HASH_KEY`. The 64 hex characters are stored in `certificate.signature`. The **signature code** printed on the page and the PDF is its first 16 characters in groups of four (`signing.code`), for people to compare by eye; it is not used to look a certificate up.
- **The id is the credential.** `secrets.token_urlsafe(16)`: 22 characters, 128 random bits. The check link is `PUBLIC_BASE_URL/check/{id}`, and the QR code on the PDF encodes it. It doesn't expire: it works until the volunteer withdraws the certificate, issues a new one for the same nonprofit and period, or deletes the account. (In a demo, dropping the oldest demo volunteer copy deletes its certificates outright, so their links then read "Not a Causematch record".) The route is public (`PUBLIC` in `app/auth.py`), limited per address, and its answers carry `noindex` (meta and `X-Robots-Tag`), `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. The app's own logs never hold the id (see the error bullet above). Cloud Run's request log is outside the app and records the full URL, as `docs/OPERATIONS.md` says of the preview key and the OAuth code, so anyone who can read the project's logs can read check links.
- **Tampering.** `data.check_certificate` reads the stored row and recomputes the signature from its snapshot and id (`signing.verify`, a constant-time compare). Any change to what the snapshot says, or a signature made with another key, no longer verifies (the stored JSON is parsed and re-encoded canonically first, so whitespace or key order alone changes nothing), and the check link then gives the same 404 "Not a Causematch record" as an id that doesn't exist or has a character wrong, so a probe learns nothing from the difference. The volunteer's own page and PDF refuse such a certificate too.
- **Forgery.** The check page shows what Causematch stored, never what a document claims, so an edited PDF or printout fails as soon as someone opens its link: a made-up id has no row, and a real id shows the real name, nonprofit and total. Making a row that verifies takes the key. The checker should compare the page with the paper they were given; the signature code makes that quick.
- **Corrections after issue.** The certificate keeps what was issued. When the nonprofit later corrects one of its shifts, the check link says "Hours have changed" and still shows the issued figures.
- **The key.** Required in production and preview (32+ characters), checked at startup by `main.py`; development and test fall back to a fixed dev-only default. Changing it makes every certificate issued before read as "Not a Causematch record", and their PDFs are refused: no old key is kept for checking. `docs/OPERATIONS.md` (Rotating `CERTIFICATE_KEY`) says not to rotate it without building that first.

### Secrets and what never enters git

- **Secrets come from the environment**, through Secret Manager on Cloud Run: `SESSION_SECRET`, `PHONE_HASH_KEY`, `CERTIFICATE_KEY`, `TASKS_TOKEN`, `PREVIEW_PASSWORD`, Twilio and Resend keys, `SMTP_PASSWORD`, `GOOGLE_CLIENT_SECRET` and `APPLE_PRIVATE_KEY`. In production the first four and the Google secret must be at least 32 characters, and `SMTP_PASSWORD` at least 16 (a Gmail app password is 16 letters).
- **`PHONE_HASH_KEY`** keys several HMACs: rate-limit keys, retained phone and identity hashes after deletion, the digest of the mailbox a browser asked an emailed link for, and urgent unsubscribe links.
- **Excluded by `.gitignore`:**
  - `data/`, `*.db`
  - `.env*`, except `.env.example`
  - screenshots
  - `preview-seed/`, the real-nonprofit seed copied in only for a preview build; `.gcloudignore` re-includes it for Cloud Build
  - agent files (`.omc/`, `CLAUDE.md`, `.claude/`)
- **Secret scanning:** `scripts/secret_scan.sh` runs gitleaks over the whole history. Locally it skips when gitleaks isn't installed; in CI a missing gitleaks fails the job.
- **Seed safety:** `scripts/deploy_preview.sh` refuses to copy the seed if `preview-seed/` is tracked or not ignored.
- **Account deletion** (`data.delete_account`) soft-deletes the profile (`deleted_at`) and ends every session. It:
  - **deletes** the volunteer's places, login tokens (those for its emailed-link address too, so a link already sent can't sign it up again), identities (the emailed link's, Google's and Apple's) and photos;
  - **withdraws** every certificate of theirs and erases its `snapshot` and `signature`, so each check link says only "withdrawn"; the row keeps `id`, `volunteer_id`, `org_id`, `period`, `minutes`, `issued_at` and `withdrawn_at`, with no name;
  - **sets to NULL** on the profile 17 columns: `phone`, `email`, `display_name`, `last_name`, `contact_phone`, `lat`, `lon`, `neighborhood`, `other_causes`, `language_other`, `free_dates`, `verified_hours`, `name_changed_at`, `pronouns`, `bio`, `interests` and `photo_key`;
  - **withdraws** pending and accepted matches (cancelling their future shifts), replaces the volunteer's messages with "[message removed]" (the app's own lines carry no name and stay), and cancels their pending and failed notices;
  - **keeps** a keyed hash of the phone, of each identity and of the verified mailbox behind a Google or Apple one, only when a block, report or suspension must survive (see section 9);
  - **leaves on the deleted row** every other column, including the preference answers: `causes`, `cause_prefs`, `availability`, `dealbreakers`, `weekly_limit`, `skills`, `languages`, `setting`, `effort`, `group_size`, `first_timer`, `orientation_ok`, `horizon_days`, `commitment`, `surprise`, `frequency`, `radius_km`, the `urgent_*` settings, `heard_from` and `volunteered_last_year`. Nothing on that row names the person any more. The metrics read only `heard_from` from it (`app/metrics_rows.py`). The `delete_account` docstring says more is cleared ("every personal column scrubbed, kept: the answers the metrics group by"), and doesn't mention certificates; this list is what the code does.
- **A coordinator's deletion** (`data.remove_coordinator`) leaves their name in the snapshots of certificates already issued, as the privacy page says: those certificates record who confirmed the hours. Certificates issued later name the org instead.

---

## 13. Environments and deploy

`CAUSEMATCH_ENV` must be declared. With it unset, the app refuses to start unless it is on Cloud Run (`K_SERVICE` set), which always counts as production. A preview is production plus a password gate and a few demo allowances (`app/config.py`).

| | development / test | preview | production |
|---|---|---|---|
| Set by | `CAUSEMATCH_ENV=development` or `test` | `CAUSEMATCH_ENV=preview` + `PREVIEW_PASSWORD` (16+ chars) | `CAUSEMATCH_ENV=production`, or Cloud Run |
| Production rules (strong secrets, https origin, `__Host-` cookie, HSTS, plain 500 page) | no | yes | yes |
| Session secret, certificate key | public dev defaults if unset | required, 32+ chars | required, 32+ chars |
| Fake SMS (Me's add-a-number code, always `000000`) and fake email (logged) | allowed with `CAUSEMATCH_FAKE_SMS=1` / `CAUSEMATCH_FAKE_EMAIL=1` | allowed, and set by the deploy script | refused at startup |
| Fake Google (and Apple) consent page | allowed with `CAUSEMATCH_FAKE_OAUTH`: `1` is Google alone, `google,apple` names both (the tests set `google,apple`, `scripts/demo.py` sets `1`) | allowed, and set to `1` by the deploy script | refused at startup |
| Volunteer sign-in | the emailed link (the fake emailer logs it), plus a provider when configured | the emailed link, demo addresses only, plus the fake Google | the emailed link, so an email sender (Resend or SMTP) is required; Google and Apple are optional and offered only when configured; Twilio plays no part |
| Who can sign in | anyone | emailed links to the demo's addresses only, staff through a demo coordinator only, and a fake Google account per browser for volunteers, with any typed email (section 12) | anyone |
| Password on every request | no | yes, `PreviewGate` | no |
| Banner on every page | only with `CAUSEMATCH_DEMO_BANNER=1` (`scripts/demo.py --seed-file`) | always ("Preview ... demo data, wiped at any time · no real details") | refused at startup |
| Demo matches: a fresh demo volunteer per tap of "Try it as a volunteer" (arriving with a confirmed shift), "Try it as a nonprofit" and "Try it as an admin" buttons, and stand-in coordinators that accept a like after a few seconds | with `CAUSEMATCH_DEMO_MATCHES=1` (`scripts/demo.py`) | with `CAUSEMATCH_DEMO_MATCHES=1`, set by the deploy script | refused at startup |
| Seeding | invented orgs from `app/db.py` into an empty database (verified in dev and test) | `app/demo_seed.py` into an empty database whenever an instance starts: `PREVIEW_SEED_DIR` (kept outside git) plus one invented demo nonprofit, else the invented orgs, plus demo people | none |
| Sign-in links (volunteers' and staff's) | emailed by the fake, i.e. logged | shown on the result page for a demo address (never an admin's); demo coordinator and admin links in the startup log | emailed by Resend or Gmail |

Local options:

- `uv run python main.py`: run the app.
- `uv run python scripts/demo.py [--seed-file PATH]`: a throwaway database with demo data. It prints how to sign in as each role and refuses production.
- `scripts/screenshots.py --out DIR`: screenshot every page of the demo.
- `PREVIEW_PASSWORD=... uv run python scripts/smoke_preview.py https://<service>.run.app`: after each preview deploy, drives the presenter's path in a phone-sized browser, from the password gate through a demo volunteer's match, hours and certificate, its public check link, the demo nonprofit's shifts, the privacy page and "Try it as an admin" with the seeded claim. It prints `ok` or `FAIL` per step, never the password.
- `scripts/video/make_demo_video.sh`: re-record the narrated demo video for the website (steps in `scripts/video/README.md`).
- `scripts/build_site.sh [DIR]`: assemble the public website (`site/` plus shared assets) into `_site/`.

### What is live and what is planned

Based on the code and config in this repo:

| Piece | Status |
|---|---|
| Cloud Run preview (`causematch-preview`, `us-west1`) | **Live.** Password-protected, demo data, deployed by `scripts/deploy_preview.sh`, with one instance kept warm and a startup CPU boost. `scripts/smoke_preview.py` checks a deploy. |
| SQLite file | **Live**, but in the preview it lives inside the container and is reseeded, wiping what was there, whenever an instance starts. |
| Photo worker child process | **Live** (`app/photo_worker.py`). |
| ProPublica API | **Live.** Used by the claim page's name search, the admin orgs and claims pages and by `scripts/import_orgs.py` / `scripts/verify_org.py`. |
| Secret Manager | **Live** for the preview: `PREVIEW_PASSWORD`, `SESSION_SECRET`, `PHONE_HASH_KEY`, `CERTIFICATE_KEY`, `TASKS_TOKEN`. |
| Email: Resend, or a Gmail account over SMTP | **Implemented** (`app/notify.py`), **not live**. The preview runs the fake (`CAUSEMATCH_FAKE_EMAIL=1`). Production refuses to start without one of the two, because every sign-in link and most notices go by email. Gmail is the beta's stopgap until there is a domain to send from; it needs one instance and CPU always allocated (`--no-cpu-throttling`, which the deploy script doesn't set), as `docs/OPERATIONS.md`, Email through Gmail, explains. |
| Twilio Verify and SMS | **Implemented** (`app/notify.py`), **not live**, and optional. They only text volunteers who added a number, and prove that number. The preview runs the fakes (`CAUSEMATCH_FAKE_SMS=1`). Without Twilio nothing else changes: volunteers get email. |
| Google and Apple sign-in | **Implemented** (`app/oauth.py`, `app/routes/oauth.py`), **not live**. The preview and `scripts/demo.py` run the fake (`CAUSEMATCH_FAKE_OAUTH=1`), a local consent page for Google alone. Each real provider is offered only when configured, production needs neither (the emailed link is always there), and one half set refuses to start. Setup is in `docs/OPERATIONS.md`, Live providers. |
| Cloud Scheduler calling `/tasks/reminders` and `/tasks/urgent` | **Not set up.** The endpoints exist and check a bearer token (`app/routes/tasks.py`), but nothing calls them: the Cloud Scheduler API isn't enabled in the project, and no script creates the jobs `docs/OPERATIONS.md` describes. In the preview they can't be called at all, because `PreviewGate` and the bearer check both read the one `Authorization` header. So the preview sends no day-before reminders, no accept-notice retries and no urgent waves (the demo seed sends one wave directly). |
| Litestream to GCS | **Planned.** Mentioned only in a code comment (`app/db.py`, `Photo`). No configuration exists in the repo. |
| Public website on GitHub Pages | Published by `.github/workflows/pages.yml` on pushes to `main` that touch `site/` or its assets. A static page with the demo video; it doesn't talk to the app. |

The app runs as a single process whose state is local to the instance: the SQLite file on the container's disk, plus in memory the open chat sockets (`app/routes/chat.py`) and the token buckets (`app/security.py`). The preview deploy caps the service at one instance (`--max-instances 1`). Nothing sets that cap for production, which would need it (or shared state).

### Container and preview deploy

```mermaid
flowchart LR
    dev["Developer machine<br/>scripts/deploy_preview.sh"] -- "checks project is named Causematch<br/>copies seed into ignored preview-seed/" --> cb["Cloud Build<br/>gcloud run deploy --source ."]
    cb -- "Dockerfile" --> img["Image in Artifact Registry<br/>python:3.12-slim + uv, pinned by digest<br/>uv sync --frozen --no-dev<br/>non-root user owns only data/"]
    img --> svc["Cloud Run service causematch-preview<br/>us-west1, 1 warm instance (MIN_INSTANCES), at most 1<br/>1 GiB, 1 CPU, startup CPU boost<br/>--allow-unauthenticated, the app gates itself"]
    sm["Secret Manager<br/>PREVIEW_PASSWORD, SESSION_SECRET,<br/>PHONE_HASH_KEY, CERTIFICATE_KEY, TASKS_TOKEN"] --> svc
    svc -- "second step: set PUBLIC_BASE_URL<br/>to the service URL" --> svc
```

- **`Dockerfile`:** the image's `CMD` runs uvicorn with the `Procfile`'s flags (`--ws-max-size 65536`, `--limit-concurrency 200`, `--timeout-keep-alive 15`, `--timeout-graceful-shutdown 8`, `--no-proxy-headers`) on `$PORT`, default 8080.
- **`scripts/deploy_preview.sh`** sets `CAUSEMATCH_ENV=preview`, `PREVIEW_SEED_DIR=/app/preview-seed`, `TRUSTED_PROXY_HOPS=1`, the three fakes (SMS, email, and Google through `CAUSEMATCH_FAKE_OAUTH=1`), `CAUSEMATCH_DEMO_MATCHES=1`, `CONTACT_EMAIL` and `PUBLIC_BASE_URL`, deploys with `--min-instances "$MIN_INSTANCES"` (default 1, so a demo never waits for a cold start of about 50 seconds; `MIN_INSTANCES=0` lets it scale to zero when idle), `--max-instances 1` and `--cpu-boost`, and reads `PREVIEW_PASSWORD`, `SESSION_SECRET`, `PHONE_HASH_KEY`, `CERTIFICATE_KEY` and `TASKS_TOKEN` from Secret Manager. The seed also ends up in the Cloud Build source bucket and in the image. Both are private to the project.
- **Production is not deployed yet.** The code expects these additions:
  - an email sender: Resend credentials, or for the beta a Gmail account's app password (`docs/OPERATIONS.md`, Email through Gmail: one instance, CPU always allocated, the daily purge and its alerts). Google, Apple and Twilio are optional (`docs/OPERATIONS.md`, Live providers)
  - a persistent database (Litestream replication to GCS is the plan)
  - the two Cloud Scheduler jobs (`/tasks/reminders` hourly, `/tasks/urgent` every 10 minutes): the API is not enabled in the project and no script creates them. A preview would also need a way past `PreviewGate` for them, since the gate and the bearer check share the `Authorization` header.
  - at least 1 GiB of memory, for two worst-case photo decodes at once

### Environment variables

| Name | Purpose | Required |
|---|---|---|
| `CAUSEMATCH_ENV` | `development`, `test`, `preview` or `production` | always, except on Cloud Run |
| `SESSION_SECRET` | Signs the session cookie | production, preview (32+) |
| `PUBLIC_BASE_URL` | The app's origin: emailed links, the same-origin check, CSP. Never read from the request. | production, preview (`https://`, origin only); dev defaults to `http://localhost:5001` |
| `TASKS_TOKEN` | Bearer token for `/tasks/*` | production, preview (32+) |
| `PHONE_HASH_KEY` | HMAC key: rate-limit keys, retained phone and identity hashes, the mailbox digest in the session, unsubscribe links | production, preview (32+) |
| `CERTIFICATE_KEY` | HMAC key that signs hours certificates. Changing it makes every certificate issued before read as not genuine. | production, preview (32+); dev and test default to a fixed dev-only key |
| `CONTACT_EMAIL` | Address on `/privacy` and in claim emails | production, preview |
| `TRUSTED_PROXY_HOPS` | Proxies appending to `X-Forwarded-For`: 1 for `*.run.app`, 2 behind a load balancer | on Cloud Run |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | Texts to volunteers who added a number. Never a sign-in. | optional; these three or none |
| `TWILIO_VERIFY_SID` | Twilio Verify: the code that proves a number Me's add-a-number is saving. Without it, or without `CAUSEMATCH_COLLECT_PHONE=1`, no number can be added. | optional; needs the three above |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google sign-in | optional; both or neither (secret 32+ in production) |
| `APPLE_CLIENT_ID`, `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY` | Sign in with Apple: Services ID, team, key id, the `.p8` key's PEM | optional; all four or none; https only |
| `EMAIL_API_KEY`, `EMAIL_FROM` | Resend: every email, sign-in links included. Used whenever `EMAIL_API_KEY` is set. | Resend or SMTP, unless `CAUSEMATCH_FAKE_EMAIL=1` |
| `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_HOST`, `SMTP_PORT` | Email over SMTP, for the beta a Gmail account and its app password. Host defaults to `smtp.gmail.com`, port to 587 (STARTTLS; 465 is TLS from the start); the sender is `EMAIL_FROM`, else the username. | username and password, or none of the four; password 16+ chars in production |
| `SMTP_MAX_PER_DAY` | The ceiling on emails sent over SMTP in any 24 hours, every kind together (section 12). Default 400, at most 450 through Gmail. | optional |
| `GMAIL_PURGE` | `daily` once the Gmail account's daily purge is installed; the privacy page then says its copies go after a day. | optional; anything else refuses to start |
| `PREVIEW_PASSWORD` | The preview's access key (`?access=` link) and HTTP Basic password | preview (16+) |
| `PREVIEW_SEED_DIR` | Folder with `orgs.json`, `logos-raw/`, `photos-raw/` | optional, preview |
| `CAUSEMATCH_FAKE_SMS`, `CAUSEMATCH_FAKE_EMAIL` | `1` = fakes (the SMS fake's add-a-number code is `000000`) | optional; dev, test, preview only |
| `CAUSEMATCH_COLLECT_PHONE` | `1` = Me offers the optional mobile number (also needs Twilio Verify or the SMS fake); default off everywhere | optional |
| `CAUSEMATCH_FAKE_OAUTH` | A local consent page stands in for the providers: `1` for Google alone, or names such as `google,apple` | optional; dev, test, preview only |
| `CAUSEMATCH_DEMO_BANNER` | `1` = demo banner (set by `scripts/demo.py --seed-file`) | optional; refused in production |
| `CAUSEMATCH_DEMO_MATCHES` | `1` = "Try it as a volunteer", "Try it as a nonprofit" and "Try it as an admin" buttons, a fresh demo volunteer per tap, and stand-in coordinators that accept likes (set by `scripts/demo.py` and the preview deploy) | optional; dev, test, preview only |
| `CAUSEMATCH_DB` | SQLite path, default `data/causematch.db` | optional |
| `SMS_MAX_PER_HOUR`, `SMS_MAX_PER_DAY` | Global SMS ceiling (add-a-number codes and notices), defaults 100 / 500 | optional |
| `CAUSEMATCH_LOG_LEVEL` | `DEBUG` adds a hashed client key per limited request | optional |
| `PORT`, `K_SERVICE` | Set by Cloud Run | n/a |

`.env.example` lists the variables without values. `CAUSEMATCH_DEMO_BANNER` and `CAUSEMATCH_DEMO_MATCHES` are read in `app/config.py` but aren't listed there.

---

## 14. Testing and CI

| Part | What it covers |
|---|---|
| `tests/conftest.py` | Before importing the app it sets `CAUSEMATCH_ENV=test`, a temporary `CAUSEMATCH_DB`, the three fakes (SMS, email, and `CAUSEMATCH_FAKE_OAUTH=google,apple` for both providers) and `PUBLIC_BASE_URL=https://causematch.test`. It replaces Starlette's `TestClient` with one that sends the same `Origin` a browser would, and provides fixtures for fresh databases and signed-in volunteers and coordinators. The `live_url` fixture runs the real app under uvicorn in a thread on a free port, for browser tests. |
| `tests/test_*.py` (34 modules) | One module per area: access and roles, auth (the emailed link: limits, the confirm page, suspended mailboxes), Google and Apple sign-in and one account per address (`test_oauth.py`, `test_oauth_apple.py`, through the fake consent page and with test-generated signing keys), cookie settings, org auth, claims, deck, matching (`test_match*.py`), prefs, onboarding, openings, urgent calls, shifts (marking, confirmed hours, cancel and confirm pages), My hours and certificates (`test_hours.py`: issuing, the name rules, the PDF, tampering, withdrawal, the check link and its limit), chat and threads, notify (including the Gmail sender and its ceilings), safety, security (including the migrations: the oldest schema must reach a new database's columns), preview (including the demo-only rules), photos, org media, profile, metrics, import, db and migrations, PWA, UI, and the public website (`test_site.py`). |
| `tests/e2e/` | Playwright with Chromium, often at iPhone 13 size, against `live_url`: swipe and drag, live chat between two browsers, the match moment arriving live, the deck fitting one screen, preferences, photo upload, layout and tap targets. Marked `e2e`. |
| `tests/fixtures/propublica/` | Recorded ProPublica API responses, so tests never call the network. |
| `tests/fixtures/schema_since_baseline.sql` | Every table as `app/db.py` first created it since the migrations baseline, schema only. History doesn't change, so it is never regenerated. |
| `scripts/smoke_preview.py` | Not part of CI: a Playwright run against a deployed preview, after each deploy (section 13). |

`pyproject.toml` sets a 300 s per-test timeout (`pytest-timeout`) and the `e2e` and `perf` markers.

CI (`.github/workflows/ci.yml`) runs on pushes to `main` and on pull requests, on `ubuntu-latest`, in this order:

```mermaid
flowchart LR
    a["checkout<br/>full history"] --> b["uv sync --frozen<br/>playwright install chromium"]
    b --> c["Lint<br/>ruff check<br/>ruff format --check"]
    c --> d["Secret scan<br/>gitleaks 8.21.2, checksum-verified<br/>scripts/secret_scan.sh"]
    d --> e["Tests<br/>pytest -q -rs<br/>unit, integration and e2e"]
    e --> f["Photo worker memory cap<br/>the two RLIMIT tests must pass,<br/>not skip, on Linux"]
```

A second workflow, `.github/workflows/pages.yml`, only assembles and publishes the website.

---

## 15. Where to go next, and a glossary

### Where to go next

- **New to the code:** read Part 1 first, then `main.py`, then `app/auth.py` and `app/security.py`, then one vertical slice. A good one is `app/routes/deck.py`, then the deck functions in `app/data.py`, then `app/match.py`.
- **Changing the schema:** see section 9 and the docstring in `app/migrations/__init__.py`. A new table needs only its class; anything else, a new column included, is a numbered migration, or the app won't start.
- **Adding a POST route:** it gets the same-origin check automatically. Add the route to `PUBLIC` in `app/auth.py` only if it must work signed out. A new upload path needs an entry in `security.UPLOADS`, and a new scheduler path needs `ORIGIN_EXEMPT` plus its own bearer-token check.
- **Adding Tailwind classes:** run `scripts/build_css.sh` and commit `static/vendor/app.css`.
- **Operations:** see `docs/OPERATIONS.md` (configuration, email through Gmail, scheduled jobs, security notes, preview deploy) and `scripts/`.
- **Running the admin pages:** `docs/ADMIN_GUIDE.md`.

### Glossary

| Term | Meaning |
|---|---|
| **Opening** | A volunteering opportunity a nonprofit posts: title, cause, place, next start time, shift length, recurrence, slots. Status is `open`, `filled` (accepted matches reached slots) or `closed` (it can be reopened while its start is ahead). A coordinator can edit it while open or filled. |
| **Card / deck** | The volunteer's ranked stack of openings, shown one card at a time. |
| **Swipe** | A Like or Pass on a card. There is one per volunteer and opening, and a card never comes back. |
| **Like** | A swipe right. It creates a pending match. |
| **Match** | A like and its outcome: `pending`, `accepted`, `declined` or `withdrawn`. |
| **Accept** | The coordinator's yes. It books the first shift (when its start is still ahead), tells the volunteer (email, or text) and opens the chat. |
| **Undo window** | The two minutes after a coordinator's Decline (`match.undo_until`) in which it can be taken back, and in which the volunteer is shown nothing of it. |
| **Shift** | One dated occurrence of volunteering on an accepted match, with its own length. Status is `scheduled`, `completed` (attended), `no_show` or `cancelled`. The first is created on accept (`kind=accept`), and later ones are added by the coordinator (`kind=added`). |
| **Shift length** | How long a shift lasts (`duration_minutes`, 1 to 8 hours), set on the opening and copied to each shift, or picked by the coordinator when adding one. Old rows have none. |
| **Still coming?** | The day-before reminder's question. A yes stamps `shift.confirmed_at`, and the coordinator sees "Confirmed". |
| **Can't make this one** | A volunteer cancelling one upcoming shift. The match stays, except for the only shift of a one-time opening. |
| **Refill** | The urgent call for 1 person the app posts itself when a late cancel gives up a spot (`urgent_call.refill`). |
| **Confirmed hours** | The hours a coordinator stored when marking a shift attended (`shift.confirmed_minutes`). They add up on the volunteer's Shifts and hours page. |
| **Certificate** | A volunteer's signed record of the hours one nonprofit confirmed, for a month or for all of them (`certificate` table). Its snapshot is frozen when issued. |
| **Check link** | `/check/{id}`, the public page that says whether a certificate is genuine, has changed hours or was withdrawn. The PDF's QR code points at it. The random id is the only credential. |
| **Signature code** | The first 16 characters of a certificate's HMAC signature, grouped in fours ("A1B2-C3D4-E5F6-0718"), printed so a person can compare the paper with the check link. |
| **Name change** | A changed first or last name that was already set, stamped in `profile.name_changed_at`. Hours confirmed before it can't go on a certificate. |
| **Verified hours** | A volunteer's private "I need my hours confirmed" checkbox (`profile.verified_hours = "only"`): ticked, only nonprofits that say they can confirm hours (`org.confirms_hours`) are shown. A `prefer` stored before the checkbox reads as off. |
| **Weekly limit** | "Up to N shifts a week". A full week's openings rank last and its urgent calls skip the volunteer. |
| **Clash warning** | The "You're booked at {org} then" chip on a card that starts less than 4 hours from a booked shift. |
| **Messages to answer** | Accepted chats whose latest message from a person is the volunteer's (the app's own lines don't count). They head the org's inbox and count in its Inbox badge. |
| **Urgent call / last-minute call** | The screens say last-minute, for volunteers and nonprofits alike; the code, the routes and this document often say urgent. "Need N more for this shift in the next 48 hours" on an open opening. It triggers notification waves and can auto-accept likes. |
| **Wave** | One batch of urgent notifications. There are at most 2 per call, 30 minutes apart. |
| **Auto-accept** | An urgent call option where the first N likes are accepted without a coordinator decision. |
| **Cause rating** | Love, open, or not for me, per cause. "Not for me" always hides. |
| **Dealbreaker** | A preference set to "Only show", which hides non-fitting openings instead of only ranking them lower. Distance is on by default. |
| **Place** | Where a volunteer helps from (max 2), with a travel mode and max minutes. It is never an address. |
| **Fit score** | The weighted 0 to 1 score that orders the deck and urgent recipients (section 11). |
| **Why-chip** | A short reason on a card ("♥ Animal shelters", "You're free Sat mornings"). |
| **"Next 48h"** | Deck mode limited to openings starting within 48 hours, sorted soonest first. The code calls it today mode. |
| **Surprise me** | About one card in 8 comes from outside the volunteer's loved causes. |
| **Filter sheet / view** | Temporary deck filters kept in the session, separate from saved preferences. |
| **Coordinator / owner** | A nonprofit staff member with membership in an org. The owner is the first approved claimant. |
| **Claim** | A public request to manage an org, decided by an admin. |
| **Verified org** | An org whose EIN an admin checked. Only verified, non-deactivated orgs appear in decks. |
| **Org-blind like** | A like from a volunteer the org blocked. It's recorded but never shown to the org. |
| **Preview** | The password-protected Cloud Run deploy with demo data, fake email, SMS and Google sign-in, and emailed links and staff sign-ins limited to the demo's accounts. Its data is wiped whenever the instance restarts. |
| **Session version** | A counter on the profile. Bumping it signs out every device. |
| **Identity** | A volunteer's way in: a Google or Apple account, known by the provider's subject id and never by its email, or the emailed link (`provider = 'email'`), known by the exact lowercased address. One volunteer account can have several, and a first sign-in by a new way joins the account that holds its exact, proven address (10a). Coordinators and admins have none: their sign-in creates nothing. |
| **Holder, joiner** | `identity.authoritative`. A holder (1) is an identity whose address may hold an account for another way in to join: the emailed link's, Gmail on Google, iCloud on Apple. A joiner (2) is a Workspace account: its address is proven now, so it may join into a holder, but it holds nothing. |
| **Emailed link** | A single-use sign-in link sent to an address, valid 15 minutes. A volunteer's (`/login`) signs them in, or up if the address is new; a coordinator's or admin's (`/org/login`) signs in staff. Opening it shows a confirm page, and only its button signs anyone in. |
| **Confirm page** | The page an emailed link opens: "Sign in as {address}" and a button, with a warning when this browser didn't ask for the link or is signed in as someone else. |
| **Mailbox** | The address behind an email address once a `+tag` and, for Gmail, dots are dropped (`forms.abuse_key`). Suspensions, kept hashes, the link limits and the staff-mailbox rule go by mailbox. Joining accounts goes by the exact lowercased address instead, so `x+1@gmail.com` is its own account. |
| **Flow cookie** | `__Host-cm_oauth` (`cm_oauth` in development): the signed, 10-minute cookie that carries one Google or Apple sign-in from start to callback. |
| **Match moment** | The "It's a match!" takeover, which arrives live through `GET /deck/news`. |
| **Horizon** | How far ahead a volunteer looks for openings: this week, 2 weeks, a month or any time. |
| **Token bucket** | A per-user rate limit kept in memory: a burst allowance that refills at a steady rate. |
| **SMS ceiling** | The global hourly and daily cap on texts. Notifications over it are deferred, and urgent waves get half of it. |
| **Email ceiling** | The daily cap on emails sent over SMTP (`SMTP_MAX_PER_DAY`), split into shares by kind, with a floor kept for staff's sign-in links (`notify.EmailCeiling`). Resend has none in the app. |
