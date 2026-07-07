import { useEffect, useMemo, useRef, useState } from "react";
import {
  getVideoComposerCapabilities,
  mediaUrl,
  patchNode,
  uploadComposerAudio,
} from "../api/client";
import { useBoardStore } from "../store/board";
import { useVideoComposerStore } from "../store/videoComposer";

type ComposerClip = {
  nodeId: string;
  edgeId: string;
  title: string;
  mediaId: string | null;
};

const COMPOSER_STAGE_LABELS: Record<string, string> = {
  queued: "Đang chờ xử lý",
  loading_media: "Đang tải video và nhạc",
  preparing: "Đang chuẩn bị FFmpeg",
  normalizing: "Đang chuẩn hóa video",
  concatenating: "Đang nối các đoạn video",
  mixing_audio: "Đang ghép nhạc",
  finalizing: "Đang hoàn thiện video",
  completed: "Ghép video hoàn tất",
  failed: "Ghép video thất bại",
};

export function VideoComposerDialog() {
  const openNodeId = useVideoComposerStore((state) => state.openNodeId);
  const closeComposer = useVideoComposerStore((state) => state.closeComposer);
  const compose = useVideoComposerStore((state) => state.compose);
  const cancelComposition = useVideoComposerStore(
    (state) => state.cancelComposition,
  );
  const active = useVideoComposerStore((state) =>
    openNodeId ? state.active[openNodeId] : undefined,
  );
  const nodes = useBoardStore((state) => state.nodes);
  const edges = useBoardStore((state) => state.edges);
  const deleteEdgeByRfId = useBoardStore((state) => state.deleteEdgeByRfId);
  const updateNodeData = useBoardStore((state) => state.updateNodeData);

  const node = nodes.find((item) => item.id === openNodeId);
  const data = node?.data;
  const incoming = useMemo<ComposerClip[]>(() => {
    if (!openNodeId) return [];
    return edges
      .filter((edge) => edge.target === openNodeId)
      .map((edge) => {
        const source = nodes.find((item) => item.id === edge.source);
        if (!source || source.data.type !== "video") return null;
        return {
          nodeId: source.id,
          edgeId: edge.id,
          title: source.data.title,
          mediaId:
            typeof source.data.mediaId === "string"
              ? source.data.mediaId
              : null,
        };
      })
      .filter((item): item is ComposerClip => item !== null);
  }, [edges, nodes, openNodeId]);

  const [order, setOrder] = useState<string[]>([]);
  const [aspectRatio, setAspectRatio] = useState<"9:16" | "16:9">("9:16");
  const [audioMediaId, setAudioMediaId] = useState<string | null>(null);
  const [audioFilename, setAudioFilename] = useState<string | null>(null);
  const [keepOriginal, setKeepOriginal] = useState(true);
  const [includeMusic, setIncludeMusic] = useState(false);
  const [originalVolume, setOriginalVolume] = useState(1);
  const [musicVolume, setMusicVolume] = useState(0.2);
  const [ffmpegAvailable, setFfmpegAvailable] = useState<boolean | null>(null);
  const [uploading, setUploading] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!openNodeId || !data) return;
    const savedOrder = Array.isArray(data.videoOrder) ? data.videoOrder : [];
    const incomingIds = incoming.map((clip) => clip.nodeId);
    setOrder([
      ...savedOrder.filter((id) => incomingIds.includes(id)),
      ...incomingIds.filter((id) => !savedOrder.includes(id)),
    ]);
    setAspectRatio(data.aspectRatio === "16:9" ? "16:9" : "9:16");
    setAudioMediaId(data.audioMediaId ?? null);
    setAudioFilename(data.audioFilename ?? null);
    setKeepOriginal(data.audioMode !== "muted" && data.audioMode !== "music");
    setIncludeMusic(data.audioMode === "mix" || data.audioMode === "music");
    setOriginalVolume(data.originalVolume ?? 1);
    setMusicVolume(data.musicVolume ?? 0.2);
    setLocalError(null);
  }, [openNodeId]);

  useEffect(() => {
    if (!openNodeId) return;
    const incomingIds = incoming.map((clip) => clip.nodeId);
    setOrder((current) => [
      ...current.filter((id) => incomingIds.includes(id)),
      ...incomingIds.filter((id) => !current.includes(id)),
    ]);
  }, [incoming, openNodeId]);

  useEffect(() => {
    if (!openNodeId) return;
    void getVideoComposerCapabilities()
      .then((result) => setFfmpegAvailable(result.ffmpeg_available))
      .catch(() => setFfmpegAvailable(false));
  }, [openNodeId]);

  if (!openNodeId || !node || !data) return null;

  const clips = order
    .map((id) => incoming.find((clip) => clip.nodeId === id))
    .filter((clip): clip is ComposerClip => clip !== undefined);
  const readyClips = clips.filter((clip) => clip.mediaId !== null);
  const isRunning = !!active || data.status === "queued" || data.status === "running";
  const audioMode =
    includeMusic && audioMediaId
      ? keepOriginal
        ? "mix"
        : "music"
      : keepOriginal
        ? "original"
        : "muted";
  const canCompose =
    readyClips.length >= 2 ||
    (readyClips.length === 1 && includeMusic && !!audioMediaId);
  const progress = Math.max(
    0,
    Math.min(100, Math.round(data.assemblyProgress ?? 0)),
  );
  const showProgress =
    isRunning ||
    (data.status === "done" && !!data.mediaId) ||
    data.status === "error";
  const progressLabel =
    data.status === "error"
      ? COMPOSER_STAGE_LABELS.failed
      : data.status === "done"
        ? COMPOSER_STAGE_LABELS.completed
        : COMPOSER_STAGE_LABELS[data.assemblyStage ?? ""] ??
          "Đang xử lý video";
  const visibleError =
    localError ||
    (typeof data.error === "string" && data.error.trim()
      ? data.error
      : data.status === "error"
        ? "Ghép video thất bại nhưng backend không trả về chi tiết. Hãy thử lại sau khi restart backend."
        : null);

  async function persistOrder(nextOrder: string[]) {
    setOrder(nextOrder);
    updateNodeData(openNodeId!, { videoOrder: nextOrder });
    const nodeId = Number.parseInt(openNodeId!, 10);
    if (Number.isFinite(nodeId)) {
      await patchNode(nodeId, { data: { videoOrder: nextOrder } }).catch(
        () => {},
      );
    }
  }

  function dropOn(targetId: string) {
    if (!draggedId || draggedId === targetId) return;
    const next = order.filter((id) => id !== draggedId);
    const targetIndex = next.indexOf(targetId);
    next.splice(targetIndex, 0, draggedId);
    setDraggedId(null);
    void persistOrder(next);
  }

  async function uploadAudio(file: File) {
    setUploading(true);
    setLocalError(null);
    try {
      const nodeId = Number.parseInt(openNodeId!, 10);
      const uploaded = await uploadComposerAudio(file, nodeId);
      setAudioMediaId(uploaded.media_id);
      setAudioFilename(uploaded.filename);
      setIncludeMusic(true);
      updateNodeData(openNodeId!, {
        audioMediaId: uploaded.media_id,
        audioFilename: uploaded.filename,
      });
      await patchNode(nodeId, {
        data: {
          audioMediaId: uploaded.media_id,
          audioFilename: uploaded.filename,
        },
      });
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  }

  async function removeAudio() {
    const nodeId = Number.parseInt(openNodeId!, 10);
    setAudioMediaId(null);
    setAudioFilename(null);
    setIncludeMusic(false);
    updateNodeData(openNodeId!, {
      audioMediaId: undefined,
      audioFilename: undefined,
    });
    await patchNode(nodeId, {
      data: { audioMediaId: null, audioFilename: null },
    }).catch(() => {});
  }

  async function startComposition() {
    setLocalError(null);
    if (readyClips.length === 0) {
      setLocalError("Cần ít nhất 1 video đã tạo xong.");
      return;
    }
    if (readyClips.length === 1 && (!includeMusic || !audioMediaId)) {
      setLocalError("Với 1 video, hãy tải và bật nhạc nền trước khi ghép.");
      return;
    }
    if (includeMusic && !audioMediaId) {
      setLocalError("Hãy tải nhạc nền hoặc tắt tùy chọn chèn nhạc.");
      return;
    }
    await compose(openNodeId!, {
      videoOrder: order,
      aspectRatio,
      audioMediaId,
      audioFilename,
      audioMode,
      originalVolume,
      musicVolume,
    });
  }

  return (
    <div
      className="gen-dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !isRunning) closeComposer();
      }}
    >
      <div
        className="gen-dialog video-composer-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="video-composer-title"
      >
        <div className="video-composer__header">
          <div>
            <h2 id="video-composer-title" className="gen-dialog__title">
              Video Composer
            </h2>
            <p className="video-composer__subtitle">
              Ghép video theo thứ tự và tùy chỉnh âm thanh.
            </p>
          </div>
          <button
            type="button"
            className="video-composer__close"
            onClick={closeComposer}
            disabled={isRunning}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {data.mediaId && (
          <video
            className="video-composer__preview"
            src={mediaUrl(data.mediaId)}
            controls
          />
        )}

        {showProgress && (
          <div
            className={`video-composer__progress${
              data.status === "error"
                ? " is-error"
                : data.status === "done"
                  ? " is-complete"
                  : ""
            }`}
          >
            <div className="video-composer__progress-head">
              <span>{progressLabel}</span>
              <strong>{progress}%</strong>
            </div>
            <div
              className="video-composer__progress-track"
              role="progressbar"
              aria-label={progressLabel}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progress}
            >
              <span style={{ width: `${progress}%` }} />
            </div>
          </div>
        )}

        <section className="video-composer__section">
          <div className="video-composer__section-title">
            Video đầu vào ({readyClips.length}/{clips.length})
          </div>
          {clips.length === 0 ? (
            <div className="video-composer__empty">
              Kéo dây từ ít nhất một node Video vào node này.
            </div>
          ) : (
            <div className="video-composer__clips">
              {clips.map((clip, index) => (
                <div
                  key={clip.nodeId}
                  className={`video-composer__clip${
                    draggedId === clip.nodeId ? " is-dragging" : ""
                  }`}
                  draggable={!isRunning}
                  onDragStart={() => setDraggedId(clip.nodeId)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => dropOn(clip.nodeId)}
                >
                  <span className="video-composer__drag">⋮⋮</span>
                  <span className="video-composer__index">{index + 1}</span>
                  {clip.mediaId ? (
                    <video src={mediaUrl(clip.mediaId)} muted preload="metadata" />
                  ) : (
                    <span className="video-composer__missing">Chưa có video</span>
                  )}
                  <span className="video-composer__clip-title">{clip.title}</span>
                  <button
                    type="button"
                    onClick={() => void deleteEdgeByRfId(clip.edgeId)}
                    disabled={isRunning}
                    title="Remove this connection"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="video-composer__section video-composer__settings">
          <label>
            Tỷ lệ đầu ra
            <select
              value={aspectRatio}
              onChange={(event) =>
                setAspectRatio(event.target.value as "9:16" | "16:9")
              }
              disabled={isRunning}
            >
              <option value="9:16">Dọc 9:16</option>
              <option value="16:9">Ngang 16:9</option>
            </select>
          </label>

          <div>
            <span className="video-composer__label">Âm thanh gốc</span>
            <div className="video-composer__segments">
              <button
                type="button"
                className={keepOriginal ? "is-active" : ""}
                onClick={() => setKeepOriginal(true)}
                disabled={isRunning}
              >
                Giữ tiếng gốc
              </button>
              <button
                type="button"
                className={!keepOriginal ? "is-active" : ""}
                onClick={() => setKeepOriginal(false)}
                disabled={isRunning}
              >
                Tắt tiếng gốc
              </button>
            </div>
          </div>

          {keepOriginal && (
            <label>
              Âm lượng tiếng gốc: {Math.round(originalVolume * 100)}%
              <input
                type="range"
                min="0"
                max="2"
                step="0.05"
                value={originalVolume}
                onChange={(event) =>
                  setOriginalVolume(Number(event.target.value))
                }
                disabled={isRunning}
              />
            </label>
          )}
        </section>

        <section className="video-composer__section">
          <div className="video-composer__audio-head">
            <label className="video-composer__music-toggle">
              <input
                type="checkbox"
                checked={includeMusic}
                onChange={(event) => setIncludeMusic(event.target.checked)}
                disabled={isRunning || !audioMediaId}
              />
              Chèn nhạc nền
            </label>
            <input
              ref={fileInput}
              type="file"
              hidden
              accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,audio/*"
              onChange={(event) => {
                const selected = event.target.files?.[0];
                if (selected) void uploadAudio(selected);
                event.target.value = "";
              }}
            />
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              disabled={uploading || isRunning}
            >
              {uploading ? "Đang tải…" : audioMediaId ? "Đổi nhạc" : "Tải nhạc"}
            </button>
          </div>
          {audioMediaId && (
            <div className="video-composer__audio">
              <span>{audioFilename || "Nhạc nền"}</span>
              <audio src={mediaUrl(audioMediaId)} controls preload="metadata" />
              <button
                type="button"
                onClick={() => void removeAudio()}
                disabled={isRunning}
              >
                Xóa nhạc
              </button>
            </div>
          )}
          {includeMusic && audioMediaId && (
            <label>
              Âm lượng nhạc: {Math.round(musicVolume * 100)}%
              <input
                type="range"
                min="0"
                max="2"
                step="0.05"
                value={musicVolume}
                onChange={(event) => setMusicVolume(Number(event.target.value))}
                disabled={isRunning}
              />
            </label>
          )}
        </section>

        {ffmpegAvailable === false && (
          <div className="video-composer__error">
            Chưa có FFmpeg. Chạy scripts/install-ffmpeg.ps1 rồi restart backend.
          </div>
        )}
        {visibleError && (
          <div className="video-composer__error">{visibleError}</div>
        )}

        <div className="video-composer__footer">
          {isRunning ? (
            <button
              type="button"
              className="video-composer__cancel"
              onClick={() => void cancelComposition(openNodeId)}
            >
              Hủy ghép
            </button>
          ) : (
            <button
              type="button"
              className="video-composer__compose"
              onClick={() => void startComposition()}
              disabled={ffmpegAvailable !== true || !canCompose}
            >
              {readyClips.length === 1
                ? "Ghép nhạc vào video"
                : `Ghép ${readyClips.length} video`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
