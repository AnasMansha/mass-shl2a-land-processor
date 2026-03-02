import os
import uuid
import logging
from flask import Flask, request, jsonify
from flasgger import Swagger, swag_from

from db import init_db, insert_job
from validator import validate_job_request
from job_processor import start_job_processor
from dotenv import load_dotenv
from flask import send_from_directory


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

swagger_template = {
    "swagger": "2.0",
    "info": {
        "title": "Jobs API",
        "description": "Geo processing job API",
        "version": "1.0.0",
    },
    "basePath": "/",
    "schemes": ["http", "https"],
}


add_job_docs = {
    "tags": ["Jobs"],
    "consumes": ["application/json"],
    "produces": ["application/json"],
    "parameters": [
        {
            "name": "body",
            "in": "body",
            "required": True,
            "schema": {
                "type": "object",
                "description": "Either an object containing a 'geojson' array, or a top-level GeoJSON Feature or FeatureCollection (time still required)",
                "required": ["geojson", "time"],
                "properties": {
                    "geojson": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Array of GeoJSON (single land chunk each)",
                    },
                    "time": {
                        "type": "string",
                        "description": "ISO-8601 timestamp or unix timestamp",
                        "example": "2026-02-24T10:00:00Z",
                    },
                    "webhook_url": {
                        "type": "string",
                        "example": "https://example.com/webhook",
                        "description": "Optional webhook URL to receive job completion status",
                    },
                    "generateTiff": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, generate TIFF files in addition to PNG",
                    },
                    "resolution": {
                        "type": "integer",
                        "minimum": 10,
                        "default": 100,
                    },
                    "Metrices": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "NDVI",
                                "PSRI",
                                "NDMI",
                                "NDRE",
                                "NDWI",
                                "TRUE_COLOR",
                            ],
                        },
                        "default": [
                            "NDVI",
                            "PSRI",
                            "NDMI",
                            "NDRE",
                            "NDWI",
                            "TRUE_COLOR",
                        ],
                    },
                },
            },
        }
    ],
    "responses": {
        200: {
            "description": "Job created",
            "schema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "status": {"type": "string", "example": "pending"},
                },
            },
        },
        400: {
            "description": "Validation error",
            "schema": {
                "type": "object",
                "properties": {"error": {"type": "string"}},
            },
        },
    },
}


get_job_docs = {
    "tags": ["Jobs"],
    "parameters": [
        {"name": "job_id", "in": "path", "type": "string", "required": True}
    ],
    "responses": {
        200: {"description": "Job details returned"},
        404: {"description": "Job not found"},
    },
}


delete_job_docs = {
    "tags": ["Jobs"],
    "parameters": [
        {"name": "job_id", "in": "path", "type": "string", "required": True}
    ],
    "responses": {
        200: {"description": "Job deleted"},
        404: {"description": "Job not found"},
    },
}


list_jobs_docs = {
    "tags": ["Jobs"],
    "parameters": [
        {
            "name": "status",
            "in": "query",
            "type": "string",
            "description": "Filter by job status (pending, processing, done, failed)",
        },
        {
            "name": "since",
            "in": "query",
            "type": "string",
            "description": "ISO-8601 datetime string; return jobs created at or after this time",
        },
        {"name": "limit", "in": "query", "type": "integer", "default": 100},
    ],
    "responses": {200: {"description": "List of jobs"}},
}


load_dotenv()
client_id = os.getenv("SH_CLIENT_ID")
client_secret = os.getenv("SH_CLIENT_SECRET")
base_url = os.getenv("APP_BASE_URL") or ""

if not client_id or not client_secret:
    raise ValueError("Missing Sentinel Hub credentials in .env")


def create_app() -> Flask:
    """Create and configure Flask application with all API endpoints.

    Returns:
        Configured Flask application instance
    """
    app = Flask(__name__)
    app.config["SWAGGER"] = {
        "title": "Jobs API",
        "uiversion": 3,
        "specs_route": "/",
    }

    Swagger(app, template=swagger_template)
    init_db()

    @app.route("/job/add", methods=["POST"])
    @swag_from(add_job_docs)
    def add_job():
        """Create a new geo-processing job.

        Accepts geojson, time, optional webhook_url, and processing parameters.
        Returns job_id and status 'pending'.
        """
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({"error": "Invalid JSON body"}), 400

        ok, result = validate_job_request(data)
        if not ok:
            return jsonify({"error": result}), 400

        job_id = str(uuid.uuid4())
        insert_job(job_id=job_id, status="pending", payload=result)

        return jsonify({"job_id": job_id, "status": "pending"}), 200

    @app.route("/jobs", methods=["GET"])
    @swag_from(list_jobs_docs)
    def list_jobs():
        """List jobs with optional filters.

        Query params:
            - status: filter by job status
            - since: ISO-8601 datetime to filter created_at >= since
            - limit: maximum results to return
        """
        from db import get_jobs

        status = request.args.get("status")
        since = request.args.get("since")
        limit_param = request.args.get("limit", "100")
        try:
            limit = int(limit_param)
        except Exception:
            return jsonify({"error": "limit must be an integer"}), 400
        if limit <= 0:
            return jsonify({"error": "limit must be > 0"}), 400

        jobs = get_jobs(status=status, since=since, limit=limit)
        return jsonify({"jobs": jobs}), 200

    @app.route("/job/<job_id>", methods=["GET"])
    @swag_from(get_job_docs)
    def get_job_status(job_id):
        """Get job status and results by job ID.

        Returns job details including status, and if processed, feature results with paths.
        If job failed, returns the failure reason.
        """
        from db import get_job, get_feature_results

        job = get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found"}), 404

        response = {
            "job_id": job["job_id"],
            "status": job["status"],
            "created_at": job["created_at"],
        }

        if job["status"] in ("done", "failed"):
            feature_results = get_feature_results(job_id, base_url)

            # Check if any feature failed
            has_failures = any(r["status"] == "failed" for r in feature_results)

            if job["status"] == "done":
                if has_failures:
                    response["status"] = "partially_failed"
                    response["features"] = feature_results
                else:
                    response["status"] = "completed"
                    response["features"] = feature_results
            else:  # failed
                response["status"] = "failed"
                # Get the first error message
                for result in feature_results:
                    if result["error"]:
                        response["error"] = result["error"]
                        break
                response["features"] = feature_results

        return jsonify(response), 200

    @app.route("/job/<job_id>", methods=["DELETE"])
    @swag_from(delete_job_docs)
    def delete_job(job_id):
        """Delete a job and remove its results folder.

        Removes job from database and deletes associated result files.
        Returns 200 if successful, 404 if job not found.
        """
        from db import delete_job as db_delete_job
        import shutil

        # Delete from database
        deleted = db_delete_job(job_id)

        if not deleted:
            return jsonify({"error": "Job not found"}), 404

        # Delete results folder
        result_dir = os.path.join("results", job_id)
        if os.path.exists(result_dir):
            try:
                shutil.rmtree(result_dir)
            except Exception as e:
                app.logger.warning(
                    f"Failed to delete result directory for job {job_id}: {e}"
                )

        return jsonify({"message": "Job deleted successfully"}), 200

    @app.route("/results/<path:filename>")
    def serve_results(filename):
        return send_from_directory("results", filename)

    return app


app = create_app()
start_job_processor(app)

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    logger.info(f"Starting Geo-Processing API on {host}:{port}")
    app.run(host=host, port=port)
