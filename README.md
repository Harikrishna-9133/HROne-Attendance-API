# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.

## Setup and Run

### Prerequisites
- Python 3.11 or compatible Python version
- MongoDB running locally, or access to a MongoDB instance

### 1. Clone the repository

```bash
git clone https://github.com/Harikrishna-9133/HROne-Attendance-API.git
cd HROne-Attendance-API
```

### 2. Create and activate a virtual environment

**Windows PowerShell**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure environment variables

Copy `.env.example` to `.env`.

**Windows PowerShell**
```powershell
Copy-Item .env.example .env
```

Update `MONGO_URI` in `.env` with your MongoDB connection string if needed. Set `MONGO_DB` to your preferred database name.

For MongoDB Atlas, configure database access and network access for the machine running the application. Do not commit your credentials to GitHub.

### 5. Start the API

```bash
python -m uvicorn app.main:app --reload
```

### 6. Open the API documentation

Visit:

http://127.0.0.1:8000/docs

Use the interactive Swagger UI to explore and test the API endpoints.

