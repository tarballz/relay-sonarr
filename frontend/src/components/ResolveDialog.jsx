import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { useQueryClient } from "@tanstack/react-query";
import { api, streamAdvance, streamReattempt, streamFillGaps } from "../api.js";
import { useDialog } from "./useDialog.js";
import ProgressStream from "./ProgressStream.jsx";
import ResolutionOptions from "./ResolutionOptions.jsx";
import ConfirmDialog from "./ConfirmDialog.jsx";

// A standalone "resolve a roadblock" dialog, shared by the Operations resume
// flow and the Library "Resolve" action. Two ways to start:
//   - pass `initial` (a fallback_suggested / exhausted result) to seed the menu
//     directly — used to re-open a stalled operation.
//   - pass `autoReattempt` to kick off an in-place re-search on open and show
//     whatever menu comes back — used to resolve an already-added series.
// `series` needs {title, tvdbId} always, plus {instanceId, seriesId} when
// autoReattempt is set. Option payloads carry every id needed to dispatch.
export default function ResolveDialog({
  series,
  chainKey,
  initial = null,
  autoReattempt = false,
  onClose,
  onToast,
}) {
  const [roadblock, setRoadblock] = useState(initial);
  const [steps, setSteps] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(null);
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  const started = useRef(false);
  const dialogRef = useDialog(onClose);

  function done(msg) {
    if (msg) onToast(msg);
    qc.invalidateQueries({ queryKey: ["operations"] });
    qc.invalidateQueries({ queryKey: ["series"] });
    onClose();
  }

  function handleTerminal(res) {
    setStreaming(false);
    if (res.status === "fallback_suggested" || res.status === "exhausted") {
      setRoadblock(res);
      return;
    }
    if (res.status === "split") {
      done(
        `Split “${series.title}” — ${res.gapCount} episode${res.gapCount === 1 ? "" : "s"} ` +
          `now downloading; existing episodes kept.`
      );
      return;
    }
    if (res.status === "placed" || res.status === "added") {
      done(`Downloading “${series.title}” — release found ✓`);
      return;
    }
    done();
  }

  function runStream(fn, params) {
    setRoadblock(null);
    setSteps([]);
    setStreaming(true);
    fn(
      { ...params, title: series.title },
      {
        onStep: (e) => setSteps((s) => [...s, e]),
        onResult: handleTerminal,
        onError: (m) => {
          setStreaming(false);
          onToast(m, true);
        },
      }
    );
  }

  // Library entry: re-search the existing series in place as soon as we open.
  useEffect(() => {
    if (autoReattempt && !started.current) {
      started.current = true;
      runStream(streamReattempt, {
        tvdbId: series.tvdbId,
        instanceId: series.instanceId,
        seriesId: series.seriesId,
        chainKey,
        nextIndex: 0,
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function dispatchOption(o) {
    switch (o.action) {
      case "reattempt":
        runStream(streamReattempt, o.payload);
        break;
      case "walk_chain":
        runStream(streamAdvance, o.payload);
        break;
      case "fill_gaps":
        runStream(streamFillGaps, o.payload);
        break;
      case "leave":
        done(`Kept “${series.title}” monitored.`);
        break;
      case "remove":
        setConfirmRemove(o.payload);
        break;
      default:
        break;
    }
  }

  async function confirmRemoveSeries() {
    setBusy(true);
    try {
      await api.removeSeries(confirmRemove.instanceId, confirmRemove.seriesId);
      done(`Removed “${series.title}”.`);
    } catch (e) {
      onToast(e.message, true);
      setBusy(false);
      setConfirmRemove(null);
    }
  }

  const inFlight = streaming || steps.length > 0;
  return (
    <>
      <div className="scrim" onClick={onClose}>
        <motion.div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-label={`Resolve ${series.title}`}
          tabIndex={-1}
          className="dialog"
          onClick={(e) => e.stopPropagation()}
          initial={{ opacity: 0, y: 16, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          transition={{ duration: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
        >
          <div className="dialog-head">
            <h2>{series.title}</h2>
            <div className="meta" style={{ marginTop: 6 }}>
              <span className="mono">tvdb {series.tvdbId}</span>
            </div>
          </div>
          <div className="dialog-body">
            {inFlight && <ProgressStream steps={steps} />}
            {roadblock && (
              <ResolutionOptions data={roadblock} series={series} onDispatch={dispatchOption} />
            )}
            {!inFlight && !roadblock && (
              <div style={{ padding: "8px 0", color: "var(--ink-dim)" }}>Checking availability…</div>
            )}
          </div>
        </motion.div>
      </div>
      {confirmRemove && (
        <ConfirmDialog
          title={`Remove “${series.title}”?`}
          message="Removes the empty series. Files already on disk are kept."
          confirmLabel="Remove"
          danger
          busy={busy}
          onConfirm={confirmRemoveSeries}
          onCancel={() => setConfirmRemove(null)}
        />
      )}
    </>
  );
}
