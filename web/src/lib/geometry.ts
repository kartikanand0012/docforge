/** Boxes come from the API in PDF points with the origin at the bottom-left of the page.
 * The page image is drawn top-down, so a box is placed by percentages from the top-left. */

export type Box = { page: number; x0: number; y0: number; x1: number; y1: number };
export type PageSize = { width: number; height: number };
export type Overlay = { left: string; top: string; width: string; height: string };

const percent = (value: number): string => `${Math.max(0, Math.min(100, value)).toFixed(3)}%`;

export function toOverlay(box: Box, page: PageSize): Overlay {
  if (page.width <= 0 || page.height <= 0) {
    throw new Error("page size must be positive");
  }
  return {
    left: percent((box.x0 / page.width) * 100),
    top: percent(((page.height - box.y1) / page.height) * 100),
    width: percent(((box.x1 - box.x0) / page.width) * 100),
    height: percent(((box.y1 - box.y0) / page.height) * 100),
  };
}
