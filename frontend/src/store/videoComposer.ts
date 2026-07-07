import { create } from "zustand";
import {
  cancelActivity,
  createRequest,
  getRequest,
  patchNode,
} from "../api/client";
import { useBoardStore } from "./board";

export type ComposerAudioMode = "original" | "muted" | "mix" | "music";

export interface ComposerConfig {
  videoOrder: string[];
  aspectRatio: "9:16" | "16:9";
  audioMediaId: string | null;
  audioFilename: string | null;
  audioMode: ComposerAudioMode;
  originalVolume: number;
  musicVolume: number;
}

type ActiveComposition = {
  requestId: number;
  timerId: ReturnType<typeof setTimeout> | null;
};

interface VideoComposerState {
  openNodeId: string | null;
  active: Record<string, ActiveComposition>;
  error: string | null;

  openComposer(rfId: string): void;
  closeComposer(): void;
  compose(rfId: string, config: ComposerConfig): Promise<void>;
  cancelComposition(rfId: string): Promise<void>;
  clearError(): void;
}

export const useVideoComposerStore = create<VideoComposerState>((set, get) => ({
  openNodeId: null,
  active: {},
  error: null,

  openComposer(rfId) {
    set({ openNodeId: rfId, error: null });
  },

  closeComposer() {
    set({ openNodeId: null });
  },

  async compose(rfId, config) {
    const nodeId = Number.parseInt(rfId, 10);
    if (!Number.isFinite(nodeId)) return;

    const dataPatch = {
      videoOrder: config.videoOrder,
      aspectRatio: config.aspectRatio,
      audioMediaId: config.audioMediaId,
      audioFilename: config.audioFilename,
      audioMode: config.audioMode,
      originalVolume: config.originalVolume,
      musicVolume: config.musicVolume,
      assemblyProgress: 0,
      assemblyStage: "queued",
      error: null,
    };
    useBoardStore.getState().updateNodeData(rfId, {
      videoOrder: config.videoOrder,
      aspectRatio: config.aspectRatio,
      audioMediaId: config.audioMediaId ?? undefined,
      audioFilename: config.audioFilename ?? undefined,
      audioMode: config.audioMode,
      originalVolume: config.originalVolume,
      musicVolume: config.musicVolume,
      assemblyProgress: 0,
      assemblyStage: "queued",
      error: undefined,
      status: "queued",
    });
    try {
      await patchNode(nodeId, {
        status: "queued",
        data: dataPatch,
      });
      const request = await createRequest({
        type: "compose_video",
        node_id: nodeId,
        params: {
          video_order: config.videoOrder,
          aspect_ratio: config.aspectRatio,
          audio_media_id: config.audioMediaId,
          audio_mode: config.audioMode,
          original_volume: config.originalVolume,
          music_volume: config.musicVolume,
        },
      });
      set((state) => ({
        active: {
          ...state.active,
          [rfId]: { requestId: request.id, timerId: null },
        },
        error: null,
      }));

      let networkFailures = 0;
      const poll = () => {
        if (!get().active[rfId]) return;
        const timerId = setTimeout(async () => {
          if (!get().active[rfId]) return;
          try {
            const current = await getRequest(request.id);
            networkFailures = 0;
            if (current.status === "queued" || current.status === "running") {
              useBoardStore.getState().updateNodeData(rfId, {
                status: current.status,
              });
              await useBoardStore.getState().refreshBoardState();
              set((state) => ({
                active: {
                  ...state.active,
                  [rfId]: { requestId: request.id, timerId: null },
                },
              }));
              poll();
              return;
            }

            set((state) => {
              const next = { ...state.active };
              delete next[rfId];
              return { active: next };
            });
            if (current.status === "done") {
              await useBoardStore.getState().refreshBoardState();
              return;
            }
            if (current.status === "canceled") {
              useBoardStore.getState().updateNodeData(rfId, {
                status: "idle",
                assemblyProgress: 0,
                assemblyStage: "idle",
              });
              return;
            }
            const message = current.error || "Video composition failed";
            useBoardStore.getState().updateNodeData(rfId, {
              status: "error",
              error: message,
              assemblyStage: "failed",
            });
            set({ error: message });
          } catch (error) {
            networkFailures += 1;
            if (networkFailures < 8) {
              poll();
              return;
            }
            const message =
              error instanceof Error ? error.message : "Composer polling failed";
            useBoardStore.getState().updateNodeData(rfId, {
              status: "error",
              error: message,
              assemblyStage: "failed",
            });
            set((state) => {
              const next = { ...state.active };
              delete next[rfId];
              return { active: next, error: message };
            });
          }
        }, 1000);
        set((state) => ({
          active: {
            ...state.active,
            [rfId]: { requestId: request.id, timerId },
          },
        }));
      };
      poll();
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Could not start composition";
      useBoardStore.getState().updateNodeData(rfId, {
        status: "error",
        error: message,
      });
      set({ error: message });
    }
  },

  async cancelComposition(rfId) {
    const entry = get().active[rfId];
    if (!entry) return;
    if (entry.timerId !== null) clearTimeout(entry.timerId);
    await cancelActivity(entry.requestId).catch(() => {});
    set((state) => {
      const next = { ...state.active };
      delete next[rfId];
      return { active: next };
    });
    useBoardStore.getState().updateNodeData(rfId, {
      status: "idle",
      assemblyProgress: 0,
      assemblyStage: "idle",
    });
  },

  clearError() {
    set({ error: null });
  },
}));
