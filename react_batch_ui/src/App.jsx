import { useEffect, useMemo, useRef, useState } from "react";
import * as XLSX from "xlsx";

const DEFAULT_BACKEND_BASE_URL = "http://localhost:8002";
const COLUMN_NAME = "Exact India Jobs Link";
const POLL_INTERVAL_MS = 5000;
const STORAGE_KEY = "scrape_gignaati_session";

function parseCsvLine(line) {
  const values = [];
  let current = "";
  let inQuotes = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];

    if (char === '"') {
      if (inQuotes && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }

    if (char === "," && !inQuotes) {
      values.push(current);
      current = "";
      continue;
    }

    current += char;
  }

  values.push(current);
  return values.map((value) => value.trim());
}

function extractUrlsFromCsv(text) {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  if (!lines.length) {
    throw new Error("The uploaded CSV file is empty.");
  }

  const headers = parseCsvLine(lines[0]);
  const columnIndex = headers.indexOf(COLUMN_NAME);
  if (columnIndex === -1) {
    throw new Error(`Column "${COLUMN_NAME}" not found in uploaded file.`);
  }

  const seen = new Set();
  const urls = [];

  for (const line of lines.slice(1)) {
    const row = parseCsvLine(line);
    const url = String(row[columnIndex] || "").trim();
    if (!url || seen.has(url)) continue;
    seen.add(url);
    urls.push(url);
  }

  return urls;
}

function extractUrlsFromFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (event) => {
      try {
        if (file.name.toLowerCase().endsWith(".csv")) {
          const text = event.target.result;
          resolve(extractUrlsFromCsv(String(text || "")));
          return;
        }

        const data = new Uint8Array(event.target.result);
        const workbook = XLSX.read(data, { type: "array" });
        const firstSheetName = workbook.SheetNames[0];
        const rows = XLSX.utils.sheet_to_json(workbook.Sheets[firstSheetName], {
          defval: ""
        });

        if (!rows.length || !(COLUMN_NAME in rows[0])) {
          reject(new Error(`Column "${COLUMN_NAME}" not found in uploaded file.`));
          return;
        }

        const seen = new Set();
        const urls = [];
        for (const row of rows) {
          const url = String(row[COLUMN_NAME] || "").trim();
          if (!url || seen.has(url)) continue;
          seen.add(url);
          urls.push(url);
        }
        resolve(urls);
      } catch (error) {
        reject(error);
      }
    };
    reader.onerror = () => reject(new Error("Failed to read the uploaded file."));
    if (file.name.toLowerCase().endsWith(".csv")) {
      reader.readAsText(file);
      return;
    }
    reader.readAsArrayBuffer(file);
  });
}

export default function App() {
  const [fileName, setFileName] = useState("");
  const [urls, setUrls] = useState([]);
  const [jobId, setJobId] = useState("");
  const [jobStatus, setJobStatus] = useState(null);
  const [resultBlob, setResultBlob] = useState(null);
  const [resultFileName, setResultFileName] = useState("master_jobs.csv");
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isParsing, setIsParsing] = useState(false);
  const [showFullJobId, setShowFullJobId] = useState(false);
  const [isHydrated, setIsHydrated] = useState(false);
  const downloadUrlRef = useRef(null);

  const submitEndpoint = useMemo(
    () => `${DEFAULT_BACKEND_BASE_URL}/scrape-details-batch/jobs`,
    []
  );

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (!saved) {
        setIsHydrated(true);
        return;
      }
      const parsed = JSON.parse(saved);
      setFileName(parsed.fileName || "");
      setUrls(Array.isArray(parsed.urls) ? parsed.urls : []);
      setJobId(parsed.jobId || "");
      setJobStatus(parsed.jobStatus || null);
      setResultFileName(parsed.resultFileName || "master_jobs.csv");
      setShowFullJobId(Boolean(parsed.showFullJobId));
    } catch (storageError) {
      console.error("Failed to restore session", storageError);
    } finally {
      setIsHydrated(true);
    }
  }, []);

  useEffect(() => {
    if (!isHydrated) return;
    const payload = {
      fileName,
      urls,
      jobId,
      jobStatus,
      resultFileName,
      showFullJobId
    };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  }, [fileName, isHydrated, jobId, jobStatus, resultFileName, showFullJobId, urls]);

  useEffect(() => {
    if (!isHydrated || !jobId) return undefined;

    let cancelled = false;
    let timeoutId;

    async function poll() {
      try {
        const response = await fetch(
          `${DEFAULT_BACKEND_BASE_URL}/scrape-details-batch/jobs/${jobId}`
        );
        if (!response.ok) {
          throw new Error(`Status request failed with ${response.status}`);
        }
        const payload = await response.json();
        if (cancelled) return;
        setJobStatus(payload);

        if (payload.status === "completed") {
          const downloadResponse = await fetch(
            `${DEFAULT_BACKEND_BASE_URL}/scrape-details-batch/jobs/${jobId}/download`
          );
          if (!downloadResponse.ok) {
            throw new Error(`Download failed with ${downloadResponse.status}`);
          }
          const blob = await downloadResponse.blob();
          if (cancelled) return;
          setResultBlob(blob);
          setResultFileName(payload.file_name || "master_jobs.csv");
          return;
        }

        if (payload.status === "failed") {
          return;
        }

        timeoutId = window.setTimeout(poll, POLL_INTERVAL_MS);
      } catch (pollError) {
        if (!cancelled) {
          setError(pollError.message || "Polling failed.");
        }
      }
    }

    poll();

    return () => {
      cancelled = true;
      if (timeoutId) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [isHydrated, jobId]);

  useEffect(() => {
    if (downloadUrlRef.current) {
      URL.revokeObjectURL(downloadUrlRef.current);
      downloadUrlRef.current = null;
    }
    if (resultBlob) {
      downloadUrlRef.current = URL.createObjectURL(resultBlob);
    }
    return () => {
      if (downloadUrlRef.current) {
        URL.revokeObjectURL(downloadUrlRef.current);
        downloadUrlRef.current = null;
      }
    };
  }, [resultBlob]);

  async function handleFileChange(event) {
    const file = event.target.files?.[0];
    setError("");
    setResultBlob(null);
    setJobId("");
    setJobStatus(null);

    if (!file) {
      setFileName("");
      setUrls([]);
      return;
    }

    setFileName(file.name);
    setIsParsing(true);
    try {
      const parsedUrls = await extractUrlsFromFile(file);
      setUrls(parsedUrls);
    } catch (parseError) {
      setUrls([]);
      setError(parseError.message || "Could not parse the uploaded file.");
    } finally {
      setIsParsing(false);
    }
  }

  function handleNewSession() {
    setFileName("");
    setUrls([]);
    setJobId("");
    setJobStatus(null);
    setResultBlob(null);
    setResultFileName("master_jobs.csv");
    setError("");
    setShowFullJobId(false);
    window.localStorage.removeItem(STORAGE_KEY);
  }

  async function handleSubmit() {
    if (!urls.length) {
      setError("No URLs found to send.");
      return;
    }

    setError("");
    setResultBlob(null);
    setJobStatus(null);
    setShowFullJobId(false);
    setIsSubmitting(true);

    try {
      const response = await fetch(submitEndpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({ urls })
      });
      if (!response.ok) {
        throw new Error(`Job submission failed with ${response.status}`);
      }
      const payload = await response.json();
      setJobId(payload.job_id);
      setJobStatus({
        job_id: payload.job_id,
        status: payload.status,
        total_sites: urls.length,
        successful: 0,
        failed: 0,
        skipped: 0,
        error: ""
      });
      setResultBlob(null);
      setResultFileName("master_jobs.csv");
    } catch (submitError) {
      setError(submitError.message || "Submission failed.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="page-shell">
      <div className="bg-orb orb-left" />
      <div className="bg-orb orb-right" />

      <main className="panel">
        <section className="hero">
          <img
            className="hero-logo"
            src="https://gccera.com/assets/logo-Bxy0aCfh.png"
            alt="GCCERA logo"
          />
          <div className="eyebrow">Scrape Gignaati</div>
          <h1>Upload CSV. Get Scraped Results.</h1>
          <p>
            Upload your CSV or Excel file, start the scrape, and download the
            final CSV once the batch is complete.
          </p>
        </section>

        <section className="card">
          <div className="toolbar">
            <div className="section-title">Session</div>
            <button className="secondary-button" onClick={handleNewSession} type="button">
              New Session
            </button>
          </div>

          <div className="uploader">
            <label className="upload-button" htmlFor="excel-upload">
              <span>Open Input File</span>
              <small>.csv, .xlsx or .xls</small>
            </label>
            <input
              id="excel-upload"
              type="file"
              accept=".csv,.xlsx,.xls"
              onChange={handleFileChange}
            />
            <div className="upload-meta">
              <div>{fileName || "No file selected yet."}</div>
              <div>{isParsing ? "Parsing workbook..." : `${urls.length} URL(s) ready`}</div>
            </div>
          </div>

          <button
            className="primary-button"
            onClick={handleSubmit}
            disabled={!urls.length || isSubmitting || isParsing}
          >
            {isSubmitting ? "Submitting..." : "Submit Batch Job"}
          </button>

          {error ? <div className="message error">{error}</div> : null}
        </section>

        <section className="card split">
          <div>
            <div className="section-title">URL Preview</div>
            <div className="code-box">
              {urls.length ? (
                urls.slice(0, 12).map((url) => <div key={url}>{url}</div>)
              ) : (
                <div>No URLs loaded yet.</div>
              )}
            </div>
            {urls.length > 12 ? (
              <div className="muted">Showing 12 of {urls.length} URLs</div>
            ) : null}
          </div>

          <div>
            <div className="section-title">Job Status</div>
            <div className="status-grid">
              <StatusTile
                label="Job ID"
                value={
                  jobId
                    ? showFullJobId
                      ? jobId
                      : `${jobId.slice(0, 8)}...${jobId.slice(-6)}`
                    : "Not started"
                }
                title={jobId || "Not started"}
                mono
                clickable={Boolean(jobId)}
                onClick={() => {
                  if (!jobId) return;
                  setShowFullJobId((current) => !current);
                }}
                helperText={jobId ? (showFullJobId ? "Click to collapse" : "Click to expand") : ""}
              />
              <StatusTile label="State" value={jobStatus?.status || "idle"} />
              <StatusTile label="Total Sites" value={jobStatus?.total_sites ?? 0} />
              <StatusTile label="Successful" value={jobStatus?.successful ?? 0} />
              <StatusTile label="Failed" value={jobStatus?.failed ?? 0} />
              <StatusTile label="Skipped" value={jobStatus?.skipped ?? 0} />
            </div>

            {jobStatus?.status === "running" || jobStatus?.status === "queued" ? (
              <div className="message info">
                Polling every {POLL_INTERVAL_MS / 1000} seconds until the job is done.
              </div>
            ) : null}

            {jobStatus?.status === "failed" ? (
              <div className="message error">{jobStatus?.error || "Batch job failed."}</div>
            ) : null}

            {downloadUrlRef.current ? (
              <a
                className="download-button"
                href={downloadUrlRef.current}
                download={resultFileName}
              >
                Download Result CSV
              </a>
            ) : (
              <div className="muted">Result file will appear here after completion.</div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}

function StatusTile({ label, value, mono = false, clickable = false, onClick, helperText = "", title = "" }) {
  return (
    <button
      type="button"
      className={`status-tile${clickable ? " is-clickable" : ""}`}
      onClick={onClick}
      title={title}
      disabled={!clickable}
    >
      <span>{label}</span>
      <strong className={mono ? "mono" : ""}>{String(value)}</strong>
      {helperText ? <small>{helperText}</small> : null}
    </button>
  );
}
