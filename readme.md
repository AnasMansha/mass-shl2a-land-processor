---

# Mass Land – Geo Processing Jobs API

A Flask-based background job processing API for large-scale land analysis using **Sentinel Hub**.
The system accepts GeoJSON land chunks, processes satellite metrics (NDVI, NDMI, etc.) with minimal Processing Units (PU) usage, and generates PNG (and optional TIFF) outputs asynchronously.

---

## 🚀 How It Works

1. Client submits a job (`/job/add`)
2. Job stored in SQLite (`jobs.db`) with status `pending`
3. Background worker claims and processes jobs
4. Results are stored under `/results/<job_id>/`
5. Client polls job status (`/job/<job_id>`)
6. Optional webhook notification on completion

---

## ▶ Running the Project

```bash
py main.py
```

The API will start on:

```
http://0.0.0.0:5000/
```

Swagger UI is available at:

```
http://localhost:5000/
```

---

## 📦 Requirements

Install dependencies:

```bash
pip install -r requirements.txt
```

You must have valid Sentinel Hub credentials.

---

## 🔧 Environment Variables (.env)

Example provided in `.env.example`.

| Variable                              | Required | Description                                                                                          |
| ------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| `SH_CLIENT_ID`                        | ✅        | Sentinel Hub OAuth client ID                                                                         |
| `SH_CLIENT_SECRET`                    | ✅        | Sentinel Hub OAuth client secret                                                                     |
| `MAX_PARALLEL_PROCESSES`              | ❌        | Max number of jobs processed in parallel (default: `1`)                                              |
| `JOB_POLL_INTERVAL_SECONDS`           | ❌        | Worker polling interval for pending jobs (default: `1.0`)                                            |
| `CHUNK_AREA_M_MULTIPLIER`             | ❌        | Controls grid chunk size (resolution × multiplier). Larger = fewer, bigger chunks (default: `10000`) |
| `MAX_FETCH_PARALLEL_REQUESTS_PER_JOB` | ❌        | Max concurrent Sentinel Hub requests per feature (default: `2`)                                      |
| `APP_BASE_URL`                        | ❌        | Base URL used to build public result URLs (e.g. `http://192.168.1.10:5000`)                          |
| `DATABASE_PATH`                       | ❌        | Path to SQLite database (default: `jobs.db`)                                                         |
| `HOST`                                | ❌        | Flask host (default: `0.0.0.0`)                                                                      |
| `PORT`                                | ❌        | Flask port (default: `5000`)                                                                         |

---

## 🗂 Project Structure

```
main.py            → Flask app + API routes
job_processor.py   → Background worker loop
job_executor.py    → Core Sentinel Hub processing logic
db.py              → SQLite storage
validator.py       → Request validation
utils.py           → Processing helpers
results/           → Generated images
jobs.db            → SQLite database
```

---

## 📡 API Endpoints

### 1️⃣ Create Job

**POST** `/job/add`

Accepts:

```json
{
  "geojson": [ ... ],
  "time": "2026-02-24T10:00:00Z",
  "webhook_url": "https://example.com/webhook",
  "resolution": 100,
  "generateTiff": false,
  "Metrices": ["NDVI", "NDMI"]
}
```

Returns:

```json
{
  "job_id": "uuid",
  "status": "pending"
}
```

---

### 2️⃣ List Jobs

**GET** `/jobs?status=pending&since=2026-02-01T00:00:00Z&limit=100`

Filters:

* `status`: pending, processing, done, failed
* `since`: ISO datetime
* `limit`: max results

---

### 3️⃣ Get Job Status

**GET** `/job/<job_id>`

Returns:

* `pending`
* `processing`
* `completed`
* `partially_failed`
* `failed`

If completed:

```json
{
  "job_id": "...",
  "status": "completed",
  "features": [
    {
      "feature_idx": 0,
      "pu_consumed": 3.2,
      "paths": {
        "NDVI": {
          "png": "http://.../results/...",
          "tif": "http://.../results/..."
        }
      },
      "status": "completed"
    }
  ]
}
```

---

### 4️⃣ Delete Job

**DELETE** `/job/<job_id>`

* Removes job from database
* Deletes results folder

---

### 5️⃣ Serve Results

**GET** `/results/<path>`

Serves generated PNG/TIFF files.

---

## 🌍 Supported Metrics

* `NDVI`
* `PSRI`
* `NDMI`
* `NDRE`
* `NDWI`
* `TRUE_COLOR`

---

## 📦 Processing Model

* Each job may contain multiple GeoJSON features
* Each feature is split into grid chunks (if it exceeds a threshold defined by CHUNK_AREA_M_MULTIPLIER*resolution)
* Each chunk is processed via Sentinel Hub
* Chunks are stitched together
* PNG always generated
* TIFF generated only if `generateTiff=true`
* Transparency outside geometry
* Invalid/Cloudy pixels rendered white

---

## 🔔 Webhook Support

If `webhook_url` is provided:

POST payload sent on completion:

```json
{
  "job_id": "...",
  "status": "completed",
  "feature_results": [...]
}
```

On failure:

```json
{
  "job_id": "...",
  "status": "failed",
  "feature_results": [...],
  "error": "error message"
}
```

Timeout: 10 seconds.

---

## 🧵 Concurrency Model

* Background thread continuously polls database
* Jobs claimed atomically
* ThreadPoolExecutor used for:

  * Parallel jobs
  * Parallel chunk fetching inside each job

Backpressure implemented when max parallel jobs reached.

---

## 🗄 Database

SQLite (`jobs.db`)

Tables:

* `jobs`
* `feature_results`

Job states:

* pending
* processing
* done
* failed

---

## 📁 Results Structure

```
results/
   <job_id>/
      <feature_idx>/
         NDVI.png
         NDVI.tif (optional)
         ...
```

---

## ⚠ Validation Rules

* GeoJSON must be Polygon or MultiPolygon (single chunk only)
* `time` must not be in future
* `resolution` must be 10–1500
* Metrics must be from allowed list
* `webhook_url` must be valid http/https

---

## 🛑 Notes

* Requires valid Sentinel Hub credentials
* Not distributed / multi-instance safe (SQLite)
* Results folder auto-created
* No authentication implemented
---
