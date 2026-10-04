# Deploying the MIZAN live demo (judging: 7–22 October 2026)

Everything in this document was researched, measured or tested on 2026-10-03.
Prices come from each provider's own page or price feed (links in §3 and
§11). Nothing has been bought or deployed yet: the owner picks and pays.

---

## 1. Recommendation

**Run MIZAN on one DigitalOcean CPU-Optimized Droplet: 8 dedicated vCPUs,
16 GB RAM, Debian 12, in London (LON1).** Use systemd, Caddy and the watchdog
in `deploy/`. Cost is **$0.25/h**: $168/month (630 SAR), or about **$114
(428 SAR)** for 4–22 October.

Why this option:

* **Reliability.** It is a plain virtual machine that is always on. There
  is no sleep, idle shutdown, scale-to-zero, CPU-credit throttling or
  platform cold start. Signup is self-serve and instant, billing is
  per-second ([DO pricing](https://www.digitalocean.com/pricing/droplets)),
  and the vCPUs are dedicated, so speed does not drift with neighbours.
  The go-live path has been tested end to end (§5): on a crash the server
  comes back in 13 s, and on a hang in under 3 minutes.
* **Speed.** The bottleneck is CPU inference, not the network. As
  measured on 2026-10-03, a 1,000-word document became 111 text chunks
  (4,384 tokens), each through bge-m3: 10–26 s depending on cores (§2).
  **Update 2026-10-03:** the semantic tier now skips text the cheaper tiers
  already explained and embeds at most 1,024 tokens per check, most
  verse-like first, with drops logged. With CPU batch 4, a 1,000-word
  article takes **4.3–4.7 s on 4 vCPUs** (was 14.1–14.5 s on the same
  build). Findings are identical on every measured set
  (`docs/SEMANTIC.md` §12). 8 vCPUs were not re-measured.
  The network round trip from Riyadh is 83 ms to London, 86 ms to
  Amsterdam and 94 ms to Frankfurt
  ([WonderNetwork](https://wondernetwork.com/pings/Riyadh)), which is under
  1% of a check. Doubling the cores saves 5–10 s per document. Moving
  closer saves about 0.1 s.
* **Cost.** For the 16-day window, dedicated cores cost tens of dollars
  more than shared ones. That is cheap insurance.

**Budget variant.** The same Droplet type with 4 dedicated vCPUs and 8 GB
costs $0.125/h ($84/month, 315 SAR; about $57 / 214 SAR for the window).
It works, but checks are about 1.3–1.7× slower, and a maximum-size
document (20,000 characters) may approach the 115 s check timeout. You
can resize later: power off, change the size, power on. (2026-10-03: with
the token budget the semantic pass is bounded at ~2.5 s on 4 vCPUs whatever
the length. A 2,501-word document takes 7.1–7.5 s, so the timeout concern
no longer applies; the 4-vCPU numbers in §2 are the measured ones.)

### Why not a Middle East region

Checked on 2026-10-03:

| Region | Status |
|---|---|
| AWS Bahrain (me-south-1) and AWS UAE (me-central-1) | Damaged by strikes in March 2026. AWS says data held only in Bahrain, or only in UAE zone mec1-az2, cannot be recovered. Restoration is still in progress ([InfoQ](https://www.infoq.com/news/2026/09/aws-middle-east-data-loss/), [Help Net Security](https://www.helpnetsecurity.com/2026/09/17/aws-middle-east-outage-permanent-data-loss-bahrain-uae/)). |
| AWS Saudi region (me-south-2) | Not live (`"available":false` on the [regions page](https://aws.amazon.com/about-aws/global-infrastructure/regions_az/)). |
| Azure Saudi Arabia East | Not live. Q4 2026 per [Microsoft](https://news.microsoft.com/source/emea/2026/02/microsoft-confirms-saudi-arabia-datacenter-region-available-for-customers-to-run-cloud-workloads-from-q4-2026/). |
| Google Cloud Dammam (me-central2) | Only for KSA customers buying through CNTXT, or invoiced billing ([Google](https://docs.cloud.google.com/docs/dammam-region-access)). Google also says any customer with a KSA billing address must move to CNTXT. |
| Azure UAE North (Dubai) | Works, but Riyadh→Dubai is 95 ms, no better than Europe. |
| **Oracle Cloud Jeddah / Riyadh** | The only live, self-serve region inside the Kingdom. A good second choice if the owner already has an OCI account whose home region is Jeddah or Riyadh (§3, OCI row). Opening one now risks signup delays, "out of capacity" errors, and a home region locked at signup, with three days to go. |

---

## 2. What the server needs (measured)

### How it was measured

The project `.venv` was used: torch 2.14.1, transformers 4.57.6, CPU
forced with `MIZAN_DEVICE=cpu`.

**Mac numbers are not used for sizing, for two reasons:**

* macOS torch uses Apple Accelerate (the AMX matrix units). On the same
  chip it runs **~3.5× faster** than Linux torch.
* The Mac was ~20 GB into swap during the runs, so its latencies and RSS
  are noisy.

The sizing numbers come from a **Linux container on the same M5**:
torch 2.14.1+cpu (OpenBLAS), with vCPUs pinned via `--cpuset-cpus`.

Test inputs:

* "1,000-word" is a real 987-word islamreligion.com article with 10
  Qur'an quotations, taken from the local real corpus. It is not
  redistributed.
* The "bench doc" is the fixed 1,032-word document in `deploy/probe.py`,
  which the owner can re-run against the server.

| Configuration | Peak RSS | Model load | Short sample (37 words) | 1,000-word article | Bench doc (1,032 words) |
|---|---|---|---|---|---|
| Deterministic (`python3`, no ML) | **0.45 GB** | n/a | 0.04 s | **1.5 s** | 2.2 s |
| AI tier fp32, 2 vCPU | 2.33 GB | first check 3.5 s | 0.58 s | 20.1–21.5 s | 25.5–25.8 s |
| AI tier fp32, 4 vCPU | 2.33 GB | first check 8.4 s (cold disk) | 0.36 s | 17.1–17.8 s | 16.0–16.2 s |
| AI tier fp32, 8 vCPU | 2.37 GB | first check 2.9 s | 0.33 s | 10.2–10.5 s | 12.1–13.0 s |
| AI tier, 4 vCPU, 2,504-word doc | 2.49 GB | – | – | 39.9–43.8 s | – |
| **2026-10-03 code**, deterministic, 4 vCPU | 0.45 GB | n/a | 0.04 s | **1.66–1.73 s** | – |
| **2026-10-03 code**, AI tier, 4 vCPU (token budget 1,024) | 2.28–2.33 GB | – | 0.34–0.41 s | **4.31–4.73 s** | – |
| **2026-10-03 code**, AI tier, 4 vCPU, 2,501-word doc | 2.33 GB | – | – | **7.06–7.45 s** (deterministic 4.21–4.29 s) | – |
| same build, previous `detect.py` (before), 4 vCPU | 2.42 GB | – | 0.40–0.41 s | 14.09–14.53 s (2,501 words: 32.9–34.6 s) | – |
| + bge-reranker-v2-m3 loaded (not used by the app) | 3.99 GB | +0.5 s | – | – | – |
| Mac M5, `.venv`, CPU (Accelerate), in-process | 1.52 GB* | 1.8 s | 0.15–0.20 s | 4.8–5.0 s | – |

\* macOS `ru_maxrss` undercounts because memory is compressed and swapped.
Use the Linux figures.

Other facts from the same runs:

* **Steady RSS** of the running service on Debian 12 + systemd: **2,466 MB**.
* **Weights are memory-mapped.** RSS is 0.64 GB right after load and
  2.1 GB once every layer has been used.
* **Server start:** `/api/health` answers 0.1–0.6 s after start.
* **Model load was lazy** in the measured code: the first check after any
  restart paid **3–13 s**. The prewarm (all 5 languages) takes
  **15–19 s** cold and **~10 s** after a restart. Late on 2026-10-03,
  `app._warm_up` was changed to load the model at startup, in the
  background, after the port opens (§8). The systemd prewarm still runs
  and waits for it.
* **Disk:** venv ~1.0 GB + model 2.27 GB + `corpus.sqlite` 124 MB +
  embeddings 73 MB. **≥ 25 GB** is plenty.

**Sizing:**

* 8 GB is the minimum comfortable RAM. 4 GB runs, with swap and little
  page cache.
* 16 GB leaves room for the page cache to keep the 2.27 GB model file
  hot, and for the reranker if it is ever enabled.

**Expect a cloud vCPU to be slower than these figures.** A cloud vCPU is
one hyperthread of a 2–3.5 GHz Xeon or EPYC, and per thread it is slower
than an M5 core. Plan for **1.0–2.0× the Linux numbers**:

* **8 dedicated vCPUs:** the bench doc takes **~12–25 s**, and a short
  sample under 1 s.
* **4 vCPUs:** the bench doc takes ~16–32 s.

Confirm this on day 1 with `probe.py smoke` (§4, step 5).

**Concurrency.** `app.py` runs every check on **one** engine thread, so
checks queue: three judges submitting 1,000-word documents at once wait
~1×, 2× and 3× the single-check time. The check timeout counts time spent
in the queue (§8).

### Cheap wins tried (`deploy/experiments/`; `semantic.py` not edited)

All rows below were measured on 4 Linux vCPUs.

| Variant | 1,000-word check | RAM | Retrieval vs torch fp32 (262 real quotes, 4 languages) | Verdict |
|---|---|---|---|---|
| torch fp32 (current) | 16.5–17.9 s | 2.1–2.3 GB | – | baseline |
| torch **int8** dynamic quantization (qnnpack) | 14.0–15.2 s (**1.2×**) | 5.4 GB peak while quantizing, 2.5–3.0 GB after | Embedding cosine 0.98. **Top-1 changes for 20/262 queries (8%).** R@1 the same ±1. | **Don't.** Small gain, and it shifts candidate rankings that the published SEMANTIC numbers were measured on. |
| **ONNX Runtime fp32** (1.30.0) | 12.1–12.6 s (**1.4×**) | 1.8 GB after load, 2.9 GB peak | **Identical.** Cosine 1.0000, every top-1/top-5 identical, every finding identical. | **The only speed-up that changes no results.** Not for Oct 7: it needs a new code path in `semantic.py`, a 2.2 GB export, and a 14 s session load. Worth doing after the competition. |
| ONNX Runtime int8 | not measured | quantization was OOM-killed at 7.8 GB (exit 137) | – | Not pursued. Int8 drift was already shown above. |
| MPS fp16 (how the index was built) vs CPU fp32 | – | – | Cosine 1.0000. Top-1 identical for 261/262 queries. | **The shipped index is valid on a CPU server.** |

---

## 3. Hosting comparison (verified 2026-10-03)

USD prices are from the providers' own pages or feeds. SAR = USD × 3.75
(the riyal's fixed peg). Riyadh round-trip times are city-to-city pings from
[WonderNetwork](https://wondernetwork.com/pings/Riyadh), measured
2026-10-03; they are not measured from each provider's data centre.

| Option | vCPU / RAM | $/month | SAR/month | Nearest region to KSA (ms from Riyadh) | Ever sleeps? Cold start? | HTTPS | Gotchas | Source |
|---|---|---|---|---|---|---|---|---|
| **DigitalOcean CPU-Optimized** ★ | **8 dedicated / 16 GB** | **168** ($0.25/h) | **630** | London 83, Amsterdam 86, Frankfurt 94 | No. VM. | Caddy + Let's Encrypt (this repo) | Billing is per second (60 s minimum). Powered-off Droplets still bill; destroy to stop. 100 GB disk. | [DO](https://www.digitalocean.com/pricing/droplets) |
| DigitalOcean CPU-Optimized | 4 dedicated / 8 GB | 84 ($0.125/h) | 315 | same | No | same | 50 GB disk. | [DO](https://www.digitalocean.com/pricing/droplets) |
| DigitalOcean Basic (shared) | 4 / 8 GB, 8 / 16 GB | 48 / 96 | 180 / 360 | same | No | same | Shared CPU: speed varies with neighbours. | [DO](https://www.digitalocean.com/pricing/droplets) |
| Hetzner CPX32 (shared AMD) | 4 / 8 GB | €35.99 ($42.59) incl. IPv4 | ~160 | Nuremberg 82 | No | DIY | Prices rose ~2.5× on 15 Jun 2026. CX/CAX lines show "not available". Prices exclude VAT. | [Hetzner](https://www.hetzner.com/cloud/regular-performance/), [price change](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/) |
| Hetzner CCX13 / CCX23 (dedicated AMD) | 2 / 8 GB; 4 / 16 GB | €43.49 / €86.49 ($51.09 / $102.09) | ~192 / ~383 | Nuremberg 82 | No | DIY | Same price-rise note as CPX32. | [Hetzner](https://www.hetzner.com/cloud/general-purpose/) |
| AWS Lightsail | 2 / 8 GB | 44 | 165 | Paris 79, London 83 (no Middle East region) | No | DIY (managed cert needs an $18/mo load balancer) | **Burstable:** sustained CPU baseline 30%/vCPU, so inference gets throttled. | [Lightsail](https://aws.amazon.com/lightsail/pricing/), [baseline](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html) |
| Vultr High Performance / Optimized CPU | 4 / 8 GB shared; 4 / 8 GB dedicated | 48 / 80 | 180 / 300 | Milan 73, Paris 79 (Tel Aviv 119 is its only Middle East site) | No | DIY | Pricing pages return 403 to bots; figures are from Vultr's public API. | [api.vultr.com/v2/plans](https://api.vultr.com/v2/plans) |
| Akamai/Linode Shared / Dedicated | 4 / 8 GB | 48 / 86 | 180 / 323 | Milan 73, Paris 79 | No | DIY | No Middle East region. | [Akamai](https://www.akamai.com/cloud/pricing/europe) |
| **Oracle Cloud A1 (Always Free)** | 2 OCPU / 12 GB (Arm) | **0** | 0 | **Jeddah / Riyadh (in Kingdom)** | VM, but idle instances **may be reclaimed**: 7 days with p95 CPU < 20%, network < 20% and memory < 20%. bge-m3 uses ~2.5 GB of 12 GB (~21%), so it is probably safe, but this is untested. | DIY | Free instances only in the **home region chosen at signup**. "Out of host capacity" can last days, and Jeddah/Riyadh each have one availability domain. Free tier cut from 4/24 to 2/12 in Jun 2026 ([InfoQ](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/)). Pay-As-You-Go likely avoids reclamation (not stated by Oracle). Paid A1 is $0.01/OCPU-h + $0.0015/GB-h. torch aarch64 CPU wheel verified working (§2). | [OCI free tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm), [regions](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm) |
| Fly.io performance-2x / shared-cpu-4x | 2 dedicated / 8 GB; 4 shared / 8 GB | 103.86 / 58.61 (fra) | 389 / 220 | Frankfurt 94, Amsterdam 86 (no Middle East region) | Only if auto-stop is on, but **`fly launch` writes `auto_stop_machines="stop"` and `min_machines_running=0`**: set them to `"off"` and `auto_start_machines=false`. | Automatic (`*.fly.dev`, `fly certs`) | Shared CPUs are throttled to a 6.25% baseline. Root filesystem limit is 8 GB. Two machines are created by default. Use the bluegreen strategy to avoid downtime while the model loads. | [pricing](https://docs.fly.io/about/pricing), [autostop](https://docs.fly.io/launch/autostop-autostart), [CPU](https://docs.fly.io/machines/cpu-performance) |
| Railway Hobby | usage-based | ~42 at 4 GB used (~82 at 8 GB) | ~158 / ~308 | Amsterdam (no Middle East region) | "Serverless" sleep is a toggle, believed off by default (not in docs; a Railway employee says so). | Automatic | A volume causes downtime on every redeploy. Build timeout is not documented. | [pricing](https://railway.com/pricing), [sleeping](https://docs.railway.com/reference/app-sleeping) |
| Render | 2 CPU / 8 GB; 4 / 8 GB | 135 / 175 | 506 / 656 | Frankfurt (no Middle East region) | Paid plans "do not spin down". | Automatic | A persistent disk disables zero-downtime deploys. Health check must answer in 5 s. | [pricing](https://render.com/pricing), [FAQ](https://render.com/docs/faq) |
| Google Cloud Run, min-instances=1, instance billing | 2 vCPU / 8 GiB | 161.74 (156.52 after free tier) | ~607 | Dammam (in Kingdom), but see §1 access limits | No scale-to-zero with `min-instances=1`, but Google: "Minimum instances can be restarted at any time". | `*.run.app`; **domain mapping not available in Middle East regions** (needs a load balancer, not priced) | Container filesystem writes count against RAM. | [pricing](https://cloud.google.com/run/pricing), [min instances](https://docs.cloud.google.com/run/docs/configuring/min-instances), [domains](https://docs.cloud.google.com/run/docs/mapping-custom-domains) |
| Hugging Face Spaces CPU Upgrade (+ PRO) | 8 vCPU / 32 GB | 21.60 + 9 PRO | 81 + 34 | **US only** for personal/PRO accounts | Paid hardware never sleeps by default. A failing Space is **auto-suspended**. Every push rebuilds and restarts it. | Automatic (`*.hf.space`; custom domain needs PRO) | No uptime SLA documented. The free CPU tier sleeps after 48 h (disqualified). | [hardware](https://huggingface.co/docs/hub/spaces-gpus), [pricing](https://huggingface.co/pricing) |
| AWS EC2 Bahrain / UAE | t4g.large 2 / 8 GB | 58.62 / 59.57 (+ disk + IPv4 ≈ 64–65) | ~240 | Bahrain 29 | – | DIY | **Regions disrupted since March 2026, not usable** (§1). T-series bills extra for CPU above baseline. | [AWS feed](https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/ec2-ondemand-without-sec-sel/Middle%20East%20(Bahrain)/Linux/index.json) |
| GCP Compute Doha (me-central1) | e2-standard-2 2 / 8 GB | 59.44 (+ disk + IP ≈ 65.52) | ~246 | Doha 62 | No | DIY | A KSA billing address must move to CNTXT. | [GCP pricing](https://cloud.google.com/products/compute/pricing/general-purpose) |
| Azure UAE North (Dubai) | D2ps v6 2 / 8 GB (Arm); B2ms 2 / 8 GB | 62.49 / 72.85 (≈ 69 / 79 all-in) | ~259 / ~297 | Dubai 95 | No | DIY | B-series slows to a 30% baseline once credits run out. | [Azure retail prices API](https://prices.azure.com/api/retail/prices) |
| Alibaba Cloud Riyadh (partner region) | g9i.large 2 / 8 GB | 89.86 pay-as-you-go | ~337 | in Kingdom | No | DIY | Whether an international self-serve account can buy here was not verified. | [ECS price list](https://www.alibabacloud.com/product/ecs-pricing-list/en) |

**Not verified:**

* Railway's build timeout.
* Cloud Run egress and load-balancer cost in Middle East regions.
* Contabo's monthly price and setup fee.
* Whether OCI Pay-As-You-Go is exempt from idle reclamation.
* Whether DigitalOcean adds 15% VAT for a Saudi billing address. Budget
  for it.
* Which DO regions currently offer the 8 vCPU CPU-Optimized size. If LON1
  doesn't, pick AMS3 or FRA1.

---

## 4. Go live, step by step (≈ 30 minutes)

### What you need

* A domain or subdomain you control, e.g. `mizan.example.com`. If you
  have none, a free [DuckDNS](https://www.duckdns.org) name works.
  duckdns.org is on the Public Suffix List, so Let's Encrypt rate limits
  apply per name. sslip.io and nip.io are not on it, and their shared
  limits can run out, so avoid them.
* An SSH key, and this repository on your laptop **with
  `data/embeddings/`**. It is committed to git (77 MB); rebuilding it on a
  CPU takes hours, so do not delete it.

### Step 1: Create the server (DigitalOcean control panel)

Create → Droplets, then:

* **Region:** London (LON1). Use Amsterdam (AMS3) or Frankfurt (FRA1) if
  the size isn't offered there.
* **Image:** Debian 12 x64.
* **Size:** CPU-Optimized → 8 vCPU / 16 GB.
* **Authentication:** SSH key.
* **Monitoring:** tick it (DO's free metrics agent).
* **Hostname:** `mizan`.

Note the IPv4 address.

### Step 2: DNS

Add an **A record** `mizan.example.com → <droplet IP>` with a 300 s TTL.

* If DNS is on Cloudflare, leave it **DNS only (grey cloud)**. Cloudflare's
  proxy cuts responses at 100 s (error 524), and a long check can take
  longer than that.
* Check it with `dig +short mizan.example.com`.

### Step 3: Copy the project from your laptop (≈ 1 min, 78 MB)

```bash
cd mizan
bash deploy/push.sh root@<droplet IP>
```

This copies the code and `data/embeddings/`. It never uploads the
third-party test corpus (`data/real/`), the local review DB or caches. The
index (`data/corpus.sqlite`) is rebuilt and verified on the server. If the
Quranpedia download is unreachable, add `--corpus` to upload your verified
copy instead.

### Step 4: Provision (on the server, ≈ 5–10 min)

```bash
ssh root@<droplet IP>
MIZAN_DOMAIN=mizan.example.com MIZAN_ACME_EMAIL=you@example.com \
    bash /opt/mizan/app/deploy/provision.sh
```

It installs packages, sets up 2 GB of swap and a firewall (22, 80 and 443
only), creates the `mizan` user, a Python 3.11 venv with CPU-only torch,
and bge-m3 at the pinned revision. It then runs `scripts/setup.sh`, which
downloads, checks SHA-256 and the fingerprint, and builds the index.
Finally it installs `mizan.service` (with prewarm), the watchdog timer,
and Caddy with automatic HTTPS. It is safe to re-run.

This exact script was run end to end on Debian 12 under systemd (in a
container), with Caddy's local CA standing in for Let's Encrypt. The
successful run printed:

```
==> 3/7 Python 3.11 venv with CPU-only PyTorch
  interpreter: /usr/bin/python3.11 (Python 3.11.2)
  torch 2.14.1+cpu | transformers 4.57.6 | threads 4
[5/7] Verify the index against the reference
  ok    fingerprint c5ce4090a69fb9439ac48edf… = reference index
  AI tier available: True | model on disk: True | index languages: ['bn', 'en', 'hi', 'tl', 'ur']
==> 6/7 systemd: mizan.service + watchdog timer
  prewarm: en  check   13.1s  verdict=REFER
  prewarm: tl  check    1.3s  verdict=REFER
  ...
  prewarm: AI tier UP - 49:13 located by the semantic tier (13.1s incl. model load); total 19.3s
  systemd[1]: Started mizan.service - MIZAN - Qur'anic quotation attribution gate (public demo).
```

### Step 5: Smoke test from your laptop (and once from a phone on a Saudi mobile network)

```bash
python3 deploy/probe.py smoke --base https://mizan.example.com
```

The run through Caddy/HTTPS in the test server printed this (4 vCPUs; the
certificate shows 0 days because Caddy's test CA issues 12-hour
certificates, while Let's Encrypt shows ~89):

```
MIZAN smoke test  https://localhost  2026-10-03 17:00
  TLS certificate                 0 days (expires 2026-10-04, issuer ?)
  GET /api/health                 9 ms  status=ok index=2026-10-02 pipeline=full
  GET / (page)                    43 ms  3,585 bytes
  POST /api/check  UI sample        0.56 s  verdict=ATTRIBUTED_WITH_EDITS
  POST /api/check  AI probe         0.99 s  verdict=REFER  AI tier ACTIVE
  POST /api/check  1032-word doc  15.0 / 17.5 s  findings=10 verdict=ATTRIBUTED_WITH_EDITS
  RESULT                            PASS
```

(Captured 2026-10-03, before NEAR started referring documents. With the
current code the UI sample and the 1032-word document both report
`verdict=REFER`; every other line is unchanged.)

**Reference times for the 1,032-word doc** (Linux, M5 cores): 25.6 s at
2 vCPU, 16.1 s at 4, 12.5 s at 8, and 2.2 s deterministic. If your server
takes **more than ~30 s**, resize it up (power off → Resize → CPU and
RAM → power on → run the smoke test again).

### Step 6: External monitoring (§6)

### Step 7 (optional): Take a snapshot

Snapshot the Droplet once the smoke test passes. A rebuild then takes
minutes. DO charges per GB-month for snapshots; that price was not
verified here.

### Updating the code later

```bash
bash deploy/push.sh root@<ip> --restart
python3 deploy/probe.py smoke --base https://mizan.example.com
```

`--restart` waits for the prewarm. Expect ~10–20 s during which checks
are slower or briefly held by Caddy.

---

## 5. How it stays up

```
judge ──HTTPS──▶ Caddy :443  (Let's Encrypt, auto-renew, HTTP/2+3, HSTS, http→https)
                   │  retries the upstream for 30 s while mizan restarts
                   ▼
            127.0.0.1:8000  app.py  (systemd: Restart=always, enabled at boot)
                   ▲    ExecStartPost: probe.py prewarm (model + 5 languages,
                   │    writes /var/lib/mizan-status/ai.json)
   watchdog timer ─┘    every 1 min: /api/health; every 10 min: one real check
                        (3 health fails or 2 engine fails → restart)
   UptimeRobot ───────▶ /api/health, /_ops/ai.json, TLS expiry   (email + push)
```

| Failure | What happens | Measured in the test server |
|---|---|---|
| Process crashes or is killed | systemd restarts it after 2 s. Prewarm reloads the model. Caddy holds incoming requests instead of returning 502. | `kill -9` → a request sent mid-restart got **HTTP 200 after 2.7 s**. Fully warm again in **12.8 s**. |
| Process hangs (alive, not answering) | The watchdog sees 3 failed health checks in a row and restarts it. | `kill -STOP` at 17:00:52 → restart at 17:03:10 → **warm at 17:03:29**. |
| Engine thread wedged (health OK, checks time out) | 2 failed deep probes in a row (≤ 20 min) → restart. | Logic covered by the same script; not separately triggered. |
| AI tier fails | Answers come from the deterministic path (§7). `ai.json` changes to `"degraded"`, which alerts you. | `MIZAN_SEMANTIC=0` → `"ai_tier": "degraded"`; checks still answer (2.3 s, 521 MB). |
| Reboot or provider maintenance | Units are enabled, so it boots straight into the prewarmed service. | `systemd-analyze verify`: OK. |
| Certificate renewal | Caddy renews automatically. | – |
| Server lost | Restore the snapshot, or create a new Droplet and repeat §4 (~30 min). Update the A record (TTL 300 s). | – |

**Privacy.** Caddy is configured without an access log, so no client IP
is ever written to disk. That keeps the README's "No accounts, no IPs, no
analytics" true. The app itself logs only method, path and status.

---

## 6. Monitoring (free)

Create a free [UptimeRobot](https://uptimerobot.com/pricing/) account. The
free plan gives 50 monitors at a 5-minute interval, includes keyword
monitors and SSL-expiry monitors, alerts by email and mobile-app push, and
is described as for "hobby and non-profit projects". Then:

1. **Keyword monitor:** URL `https://mizan.example.com/api/health`.
   Alert when the keyword `"status": "ok"` is **not present**.
2. **Keyword monitor:** URL `https://mizan.example.com/_ops/ai.json`.
   Alert when the keyword `"ai_tier": "up"` is **not present**. This file
   is rewritten by the prewarm and by the watchdog every 10 minutes.
3. **HTTP monitor:** `https://mizan.example.com/` (the page judges open).
4. **SSL / domain-expiry monitor** for `mizan.example.com`.
5. Install the UptimeRobot app and add **email + push** alert contacts.
   Pause one monitor for 10 minutes to confirm that an alert actually
   reaches your phone.

On the server, for diagnosis:

* `journalctl -u mizan -f`
* `journalctl -t mizan-watchdog`
* `cat /var/lib/mizan-status/ai.json`

---

## 7. Fallback: what happens when the AI tier fails

The fallback was confirmed in code (`semantic.available()`, and
`SpanDetector.detect` in `src/mizan/detect.py`) and measured in the Linux
container.

### Model, venv or index missing

`semantic.available()` returns False. It imports nothing, so this is
cheap. The detector then runs only its deterministic tiers.

* Measured with `HF_HUB_CACHE=/nonexistent`: the 1,000-word article gave
  the same 10 findings with the same tiers (reference/lexical), in 1.5 s,
  at 454 MB.
* The 49:13 paraphrase is no longer located (`NO_QUOTES`). Only the AI
  tier can find that one.

### AI tier fails at run time

If the model fails to load or crashes during a check, the exception is
caught in `detect.py`, the check still returns 200, and the answer comes
from the deterministic path.

**Catch (fixed 2026-10-03):** `semantic.encoder()` used not to remember
the failure, so **every check retried the 2.2 GB load**. Measured with an
invalid device: +4 s per check and a 4.3 GB RSS peak. Now the failure is
remembered and retried with back-off (60 s, doubling to 1 h). Re-measured
the same way (4 vCPU container, `MIZAN_DEVICE=invalid_device`): the first
check pays the failed load once (6.9 s, 4.19 GB peak). Later checks run at
deterministic speed: 0.04–0.05 s for the short sample, 1.80–2.02 s for the
1,000-word article, AI probe not located. RSS settles at 1.58 GB, not the
0.45 GB of `MIZAN_SEMANTIC=0`, so the switch below is still the clean fix.

**Fix:** uncomment `MIZAN_SEMANTIC=0` in `/etc/mizan/mizan.env` and run
`systemctl restart mizan`. Measured result: 2.3 s per bench doc, 521 MB,
`ai.json` reports `"degraded"`, and the smoke test reports `AI tier NOT
ACTIVE`. Undo it once the cause is fixed.

### Full pipeline (`report.check_document`) raises

`app.py` falls back to its narrow quoted-span detector and labels the
response `pipeline: "fallback"` with an Arabic note.

### UI caveat (resolved 2026-10-04)

`/api/health` now carries a `semantic` block (`loaded`, `load_failure`,
`index_languages`, …), and every `/api/check` response carries
`ai_tier_available` and `ai_tier_complete`; a partial AI pass is shown to the
user as a note. `ai.json` and the probe remain the operator's view.

---

## 8. Changes recommended in other files (NOT made here)

None of these blocks the deployment above, which works around each one.

| File | Issue | Suggested change |
|---|---|---|
| `app.py` `_guard` | **Fixed 2026-10-03.** `MIZAN_ALLOWED_HOSTS` lists the public hostname(s); the app binds 127.0.0.1 and the DNS-rebinding guard refuses every other Host. provision.sh fills it from `MIZAN_DOMAIN`. | — |
| `app.py` `_warm_up` | **Already addressed on 2026-10-03 by another track.** `_warm_up` now calls `semantic.warmup(langs)` on the engine worker. All measurements in §2 predate that change. | Nothing to do. Keep `deploy/probe.py prewarm` anyway: it holds systemd's "started" state until the model is loaded, and it proves the AI tier works end to end by locating 49:13, which feeds `ai.json`. |
| `app.py` `health_payload` | **Done 2026-10-03.** `/api/health` returns `"semantic": semantic.status()` (`loaded`, `load_failure`, …). | UptimeRobot can watch `"loaded": true` directly. |
| `app.py` `on_engine` | One engine thread; the timeout includes queue time. **Partly done 2026-10-03:** a timed-out future is now cancelled (queued work is dropped). | Queue depth is not yet in `/api/health`. |
| `app.py` `/api/check` response | Doesn't say whether tier (c) ran, so a degraded answer looks like a normal one. | Include `detector.last_semantic` (used / reason). |
| `src/mizan/semantic.py` `encoder()` | After a failed load, every check reloaded 2.2 GB (+4 s, 4.3 GB peak). | **Done 2026-10-03.** The failure is remembered and retried with back-off (60 s doubling to 1 h; `MIZAN_SEMANTIC_RETRY_S`). `semantic.status()["load_failure"]` reports it. Tested in `tests/test_semantic.py`. The ONNX Runtime fp32 path (§2) is still not added. |
| `web/app.js` | `/api/check` aborts at 120 s, so the server timeout must stay below that. | Nothing to change if `MIZAN_CHECK_TIMEOUT=115` (set in `deploy/mizan.env`). Raise both together if needed. |
| `Dockerfile` (`WITH_ML=1`) | `.dockerignore` excludes `data/embeddings`, so the ML build rebuilds the index **on CPU (hours)**. There is no model prewarm either. Not used by this plan. | Let `data/embeddings` into the ML build context, e.g. a `Dockerfile.dockerignore`, and prewarm in the entrypoint. |
| `requirements-ml.txt` | On Linux, plain `pip install -r` pulls torch with **multiple GB of CUDA packages** from PyPI. | **Done 2026-10-03.** The file documents installing torch from `--index-url https://download.pytorch.org/whl/cpu` first. Checked with a dry run: pip then keeps `2.14.1+cpu` and pulls no CUDA packages. |

---

## 9. Cost

| Item | Per hour | Per month (730 h) | Oct 4 → Oct 22 (456 h) |
|---|---|---|---|
| **DO CPU-Optimized 8 vCPU / 16 GB** (recommended) | $0.25 | **$168 = 630 SAR** | **$114 = 428 SAR** |
| DO CPU-Optimized 4 vCPU / 8 GB (budget) | $0.125 | $84 = 315 SAR | $57 = 214 SAR |
| UptimeRobot free, Caddy, Let's Encrypt | – | $0 | $0 |
| Domain | – | only if you have none (DuckDNS is free) | – |

Prices exclude any VAT DigitalOcean may add for a Saudi billing address,
which was not verified. **Destroy the Droplet after judging:** powered-off
Droplets keep billing.

---

## 10. Pre-judging checklist (do it on 6 October)

- [ ] **Code frozen.** Run `push.sh --restart` with the final code. No
  pushes during Oct 7–22 except emergencies, each followed by a smoke test.
- [ ] `python3 deploy/probe.py smoke --base https://<domain>` → **PASS**
  from the laptop, with the 1,032-word doc ≤ ~25 s.
- [ ] The page opens on a phone over a **Saudi mobile network** (STC or
  Mobily) and shows the sample result. Paste one real paragraph and check
  it.
- [ ] `https://<domain>/_ops/ai.json` shows `"ai_tier": "up"`.
- [ ] **Reboot test:** `ssh root@<ip> reboot`. Within ~1 minute the smoke
  test passes again and `ai.json` is "up".
- [ ] **Crash test:** `systemctl kill -s KILL mizan` → back in ~15 s.
- [ ] All 4 UptimeRobot monitors are green, and a test alert reached the
  phone.
- [ ] TLS certificate has > 30 days left (shown by the smoke test).
  Caddy renews it on its own.
- [ ] `df -h /` shows > 5 GB free. `free -m` shows ≥ 8 GB available.
  `systemctl --failed` is empty.
- [ ] `apt-get upgrade` and reboot **on Oct 5–6**. After that, security
  updates install automatically and never reboot the server.
- [ ] Snapshot taken. Its name and the rebuild steps (§4) are written down.
- [ ] The payment card is valid through November, and a DO billing alert
  is set.
- [ ] Domain and DNS: the A record and TTL 300 are correct, and the domain
  doesn't expire in October.
- [ ] The demo link given to the organisers is exactly the `https://` URL
  that was smoke-tested.
- [ ] You know the emergency switch (§7): `MIZAN_SEMANTIC=0` in
  `/etc/mizan/mizan.env` + `systemctl restart mizan`.

---

## 11. Files and reproduction

```
deploy/
  provision.sh          fresh Debian 12 / Ubuntu 24.04 server -> live HTTPS demo (run as root on the server)
  push.sh               laptop -> server copy (code + data/embeddings; never third-party test text)
  mizan.service         systemd unit: Restart=always, prewarm before "started", hardening
  mizan.env             /etc/mizan/mizan.env: bind 127.0.0.1 + MIZAN_ALLOWED_HOSTS, timeouts, offline model cache, kill switch
  mizan-watchdog.{service,timer}, watchdog.sh   hang detection + AI-tier status every minute / 10 minutes
  Caddyfile             automatic HTTPS, restart-tolerant proxy, /_ops/ai.json, no access log
  probe.py              prewarm | check | smoke (stdlib only)
  experiments/          measurement scripts (bench_server.py, bench_encoder.py, compare_modes.py,
                        onnx_encoder.py, make_docs.py) and the throwaway Dockerfiles used above
```

Re-run the Linux measurements on any Docker host:

```bash
docker build -f deploy/experiments/Dockerfile.bench -t mizan-bench deploy/experiments
python3 deploy/experiments/make_docs.py /tmp/mizan-docs        # needs data/real/raw (local only)
docker run --rm --cpuset-cpus=0-3 -v "$PWD":/app \
  -v ~/.cache/huggingface/hub/models--BAAI--bge-m3:/hf/hub/models--BAAI--bge-m3:ro \
  -v /tmp/mizan-docs:/docs:ro mizan-bench \
  python deploy/experiments/bench_server.py --python python --docs /docs --env MIZAN_DEVICE=cpu
```

`bench_server.py` sends its review DB to a temporary directory and never
uses port 8777. SQLite still needs write access to `data/` for the
index's `-shm` file, as in any normal run.

The timings were taken on the code as it stood on 2026-10-03, while other
tracks were still editing `detect.py` and `report.py`. Re-run the smoke
test after the final code freeze.

### Sources (all accessed 2026-10-03)

* DigitalOcean: https://www.digitalocean.com/pricing/droplets
* Hetzner:
  * https://www.hetzner.com/cloud/regular-performance/
  * https://www.hetzner.com/cloud/general-purpose/
  * https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/
* AWS Lightsail:
  * https://aws.amazon.com/lightsail/pricing/
  * https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html
* AWS EC2 and Middle East status:
  * https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/ec2-ondemand-without-sec-sel/Middle%20East%20(Bahrain)/Linux/index.json
  * https://www.infoq.com/news/2026/09/aws-middle-east-data-loss/
  * https://www.helpnetsecurity.com/2026/09/17/aws-middle-east-outage-permanent-data-loss-bahrain-uae/
  * https://aws.amazon.com/about-aws/global-infrastructure/regions_az/
* Vultr: https://api.vultr.com/v2/plans and https://api.vultr.com/v2/regions
* Akamai/Linode: https://www.akamai.com/cloud/pricing/europe
* Oracle Cloud:
  * https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
  * https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm
  * https://www.oracle.com/cloud/free/faq/
  * https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/
* Google Cloud:
  * https://cloud.google.com/run/pricing
  * https://docs.cloud.google.com/run/docs/locations
  * https://docs.cloud.google.com/run/docs/configuring/min-instances
  * https://docs.cloud.google.com/run/docs/mapping-custom-domains
  * https://docs.cloud.google.com/docs/dammam-region-access
  * https://cloud.google.com/products/compute/pricing/general-purpose
* Azure:
  * https://prices.azure.com/api/retail/prices
  * https://news.microsoft.com/source/emea/2026/02/microsoft-confirms-saudi-arabia-datacenter-region-available-for-customers-to-run-cloud-workloads-from-q4-2026/
* Fly.io:
  * https://docs.fly.io/about/pricing
  * https://docs.fly.io/reference/regions
  * https://docs.fly.io/launch/autostop-autostart
  * https://docs.fly.io/machines/cpu-performance
  * https://docs.fly.io/getting-started/troubleshooting
* Railway:
  * https://railway.com/pricing
  * https://docs.railway.com/reference/app-sleeping
  * https://docs.railway.com/reference/deployment-regions
* Render:
  * https://render.com/pricing
  * https://render.com/docs/faq
  * https://render.com/docs/regions
* Hugging Face:
  * https://huggingface.co/docs/hub/spaces-gpus
  * https://huggingface.co/pricing
  * https://huggingface.co/docs/hub/storage-regions
* Alibaba Cloud: https://www.alibabacloud.com/product/ecs-pricing-list/en
* Latency: https://wondernetwork.com/pings/Riyadh
* Caddy:
  * https://caddyserver.com/docs/caddyfile/directives/reverse_proxy (passes `Host` through unchanged by default; no response timeout by default)
  * https://caddyserver.com/docs/install
* UptimeRobot: https://uptimerobot.com/pricing/
* PyTorch CPU wheels: https://download.pytorch.org/whl/cpu/torch/ (2.14.1+cpu for x86_64 and aarch64; installed and run in this test)
* Public Suffix List: https://publicsuffix.org/list/public_suffix_list.dat (duckdns.org listed; sslip.io and nip.io not)
