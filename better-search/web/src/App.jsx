import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

const STATUS = `query { status { state filesSeen filesIndexed filesSkipped filesFailed lastError priorityFolder libraryTotal progress currentFile } }`;

const SEARCH = `query ($q: String!, $fileType: FileType!, $limit: Int!) {
  search(q: $q, fileType: $fileType, limit: $limit) {
    tookMs
    hits { path name fileType page snippet score similarity }
  }
}`;

const PRIORITIZE = `mutation ($path: String!) { prioritizeFolder(path: $path) }`;
const STOP_INDEXING = `mutation { stopIndexing }`;
const RESUME_INDEXING = `mutation { resumeIndexing }`;

const FILTERS = [
  { id: "ALL", label: "All" },
  { id: "DOCUMENT", label: "Documents" },
  { id: "IMAGE", label: "Photos" },
];

async function gql(query, variables) {
  const response = await fetch("/graphql", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  const body = await response.json();
  if (body.errors?.length) {
    throw new Error(body.errors[0].message);
  }
  return body.data;
}

function highlight(text, query) {
  const tokens = query.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
  if (!tokens.length) return text;
  const pattern = new RegExp(`(${tokens.map(escapeRegExp).join("|")})`, "ig");
  const parts = text.split(pattern);
  return parts.map((part, index) =>
    tokens.includes(part.toLowerCase()) ? <mark key={index}>{part}</mark> : <span key={index}>{part}</span>,
  );
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function progressLabel(status) {
  const seen = status.filesSeen.toLocaleString();
  if (status.libraryTotal > 0 && typeof status.progress === "number") {
    return `${seen} / ${status.libraryTotal.toLocaleString()} · ${status.progress}%`;
  }
  return `${seen} files`;
}

function statusParts(status) {
  if (!status) return { label: "Starting", file: "" };
  if (status.state === "waiting-for-library") return { label: "Waiting for the main disk", file: "" };
  if (status.state === "external-drive-blocked") return { label: "External drive blocked", file: "" };
  if (status.state === "stopped") {
    return { label: `Indexing stopped · ${status.filesIndexed.toLocaleString()} indexed`, file: "" };
  }
  if (status.state === "indexing") {
    const progress = progressLabel(status);
    const prefix = status.priorityFolder && !status.currentFile ? `Indexing ${status.priorityFolder} first` : "Indexing";
    return { label: `${prefix} · ${progress}`, file: status.currentFile || "" };
  }
  const settled = `${status.filesIndexed.toLocaleString()} indexed · ${status.filesSkipped.toLocaleString()} skipped`;
  if (status.state === "watching") return { label: `${settled} · new files index automatically`, file: "" };
  return { label: settled, file: "" };
}

export function App() {
  const reduce = useReducedMotion();
  const [query, setQuery] = useState("");
  const [folder, setFolder] = useState("");
  const [fileType, setFileType] = useState("ALL");
  const [status, setStatus] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [indexAction, setIndexAction] = useState(false);

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const data = await gql(STATUS);
        if (!stop) setStatus(data.status);
      } catch (err) {
        if (!stop) setError(err.message);
      }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, []);

  useEffect(() => {
    if (!query.trim()) {
      setResult(null);
      return undefined;
    }
    let stop = false;
    const timer = setTimeout(async () => {
      try {
        const data = await gql(SEARCH, { q: query, fileType, limit: 20 });
        if (!stop) {
          setResult(data.search);
          setError("");
        }
      } catch (err) {
        if (!stop) setError(err.message);
      }
    }, 180);
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [query, fileType]);

  const busy = status?.state === "indexing" || status?.state === "waiting-for-library" || Boolean(status?.priorityFolder);

  async function refreshStatus() {
    const data = await gql(STATUS);
    setStatus(data.status);
  }

  async function toggleIndexing() {
    if (!status || indexAction) return;
    setIndexAction(true);
    try {
      await gql(status.state === "stopped" ? RESUME_INDEXING : STOP_INDEXING);
      await refreshStatus();
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setIndexAction(false);
    }
  }

  async function submitPriority(event) {
    event.preventDefault();
    const path = folder.trim();
    if (!path) return;
    try {
      await gql(PRIORITIZE, { path });
      setFolder("");
      setError("");
      const data = await gql(STATUS);
      setStatus(data.status);
    } catch (err) {
      setError(err.message);
    }
  }
  const parts = statusParts(status);
  const canToggle = status?.state === "indexing" || status?.state === "watching" || status?.state === "stopped";
  const motionProps = useMemo(
    () =>
      reduce
        ? {}
        : {
            initial: { opacity: 0, y: 8 },
            animate: { opacity: 1, y: 0 },
            exit: { opacity: 0, y: -4 },
          },
    [reduce],
  );

  return (
    <main className="app">
      <header className="top">
        <h1 className="wordmark">Search</h1>
        <p className="status" aria-live="polite">
          <motion.span
            className={busy ? "dot busy" : "dot"}
            animate={reduce || !busy ? undefined : { opacity: [0.35, 1, 0.35] }}
            transition={{ repeat: Infinity, duration: 1.6 }}
          />
          <span className="status-copy">
            {parts.file ? (
              <span className="status-file" title={parts.file}>
                {parts.file}
              </span>
            ) : null}
            <span className="status-counts">{parts.label}</span>
          </span>
          {canToggle ? (
            <motion.button
              type="button"
              className="index-toggle"
              onClick={toggleIndexing}
              disabled={indexAction}
              whileTap={reduce ? undefined : { scale: 0.96 }}
              aria-label={status.state === "stopped" ? "Resume indexing" : "Stop indexing"}
            >
              {status.state === "stopped" ? "Resume" : "Stop"}
            </motion.button>
          ) : null}
        </p>
        {status?.state === "indexing" && typeof status.progress === "number" ? (
          <div
            className="library-progress"
            role="progressbar"
            aria-valuenow={status.progress}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label="Library indexed"
          >
            <motion.span
              animate={{ width: `${status.progress}%` }}
              transition={reduce ? { duration: 0 } : { duration: 0.45, ease: "easeOut" }}
            />
          </div>
        ) : null}
      </header>
      <p className="lede">
        Finds notes, PDFs, office files, and photos on the main disk. New files are picked up as they appear.
        External drives are left alone.
      </p>
      <label className="searchbox">
        <span aria-hidden="true">⌕</span>
        <input
          type="search"
          aria-label="Search files"
          value={query}
          placeholder="roof leak, red bike, invoice…"
          onChange={(event) => setQuery(event.target.value)}
          autoFocus
        />
      </label>
      <motion.form className="priority" onSubmit={submitPriority} {...motionProps}>
        <input
          aria-label="Folder to index first"
          placeholder="Index this folder first"
          value={folder}
          onChange={(event) => setFolder(event.target.value)}
        />
        <button type="submit">First</button>
      </motion.form>
      <div className="filters" role="tablist">
        {FILTERS.map((filter) => (
          <button
            key={filter.id}
            type="button"
            className={fileType === filter.id ? "active" : ""}
            onClick={() => setFileType(filter.id)}
            aria-pressed={fileType === filter.id}
          >
            {filter.label}
          </button>
        ))}
      </div>
      <p className="meta">
        {error ? <span className="error">{error}</span> : result ? `${result.hits.length} results · ${result.tookMs} ms` : ""}
      </p>
      {status?.lastError && status.state === "external-drive-blocked" ? (
        <p className="error">{status.lastError}</p>
      ) : null}
      <section className="results">
        <AnimatePresence mode="popLayout">
          {(result?.hits ?? []).map((hit) => (
            <motion.a
              className="result"
              key={hit.path}
              href={`/content?path=${encodeURIComponent(hit.path)}`}
              target="_blank"
              rel="noreferrer"
              layout
              {...motionProps}
            >
              {hit.fileType === "image" ? (
                <img className="thumb" alt="" src={`/content?path=${encodeURIComponent(hit.path)}`} />
              ) : (
                <div className="thumb placeholder">{hit.name.slice(0, 1).toUpperCase()}</div>
              )}
              <div>
                <h2>{hit.name}</h2>
                <p className="path">
                  {typeof hit.similarity === "number" ? (
                    <span className="confidence" title="Similarity">
                      {Math.round(hit.similarity * 100)}%
                    </span>
                  ) : null}
                  <span>
                    {hit.path}
                    {hit.page ? ` · page ${hit.page}` : ""}
                  </span>
                </p>
                <p className="snippet">{highlight(hit.snippet, query)}</p>
                <div className="bar" aria-hidden="true">
                  <span style={{ width: `${Math.round((hit.similarity ?? 0) * 100)}%` }} />
                </div>
              </div>
            </motion.a>
          ))}
        </AnimatePresence>
      </section>
    </main>
  );
}
