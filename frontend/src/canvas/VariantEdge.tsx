import {
  BaseEdge,
  EdgeLabelRenderer,
  getBezierPath,
  type EdgeProps,
} from "@xyflow/react";
import { useBoardStore } from "../store/board";

/**
 * Edge variant: draws the standard bezier line plus a small chip at the
 * midpoint when the edge has a variant pin.
 *
 * The pin (`data.sourceVariantIdx`) records which variant of the
 * upstream multi-variant node this edge consumes — set when the user
 * clicks a specific variant tile to bind it to a downstream. The label
 * surfaces that binding so it stays visible on the graph instead of
 * being hidden in node data.
 *
 * Edges without a pin (single-variant sources, or unconfigured
 * multi-variant edges still defaulting to mediaId) render exactly the
 * way the previous default edge did — only the chip is additive.
 */
export function VariantEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style,
  markerEnd,
  data,
  selected,
}: EdgeProps) {
  const deleteEdgeByRfId = useBoardStore((s) => s.deleteEdgeByRfId);
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });

  const pin = (data?.sourceVariantIdx ?? null) as number | null;
  const hasPin = pin !== null && pin >= 0;

  return (
    <>
      <BaseEdge id={id} path={edgePath} style={style} markerEnd={markerEnd} />
      {(hasPin || selected) && (
        <EdgeLabelRenderer>
          <div
            className="variant-edge-label"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
          >
            {hasPin && <span className="variant-edge-pin">v{pin + 1}</span>}
            {selected && (
              <button
                type="button"
                className="variant-edge-delete"
                title="Delete connection"
                aria-label="Delete connection"
                onClick={(event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  void deleteEdgeByRfId(id);
                }}
              >
                ×
              </button>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}
