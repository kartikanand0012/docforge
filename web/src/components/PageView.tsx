"use client";

import { useEffect, useRef } from "react";
import type { FieldAssessment } from "@/lib/api";
import { toOverlay, type Box } from "@/lib/geometry";

type Props = {
  src: string;
  page: { number: number; width: number; height: number };
  fields: FieldAssessment[];
  selected: string | null;
  /** Boxes of a chat answer's quote to outline on this page. */
  cited?: Box[];
};

/** One page of the original as uploaded: the selected value's source filled, values that
 * need attention outlined. Where a value is, is also announced in words by the review screen. */
export default function PageView({ src, page, fields, selected, cited = [] }: Props) {
  const selectedMark = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    selectedMark.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [selected]);

  if (page.width <= 0 || page.height <= 0) {
    return <p className="error">Page {page.number} has no size, so its values cannot be shown on it.</p>;
  }
  const marks = fields.flatMap((field) =>
    field.boxes
      .filter((box) => box.page === page.number)
      .filter(() => field.path === selected || field.needs_review)
      .map((box, i) => ({ key: `${field.path}-${i}`, box, field })),
  );
  return (
    <figure className="page">
      {/* eslint-disable-next-line @next/next/no-img-element -- served by the API, sized by the page */}
      <img src={src} alt={`Page ${page.number} of the original document`} width={Math.round(page.width)} height={Math.round(page.height)} />
      {marks.map(({ key, box, field }) => {
        const isSelected = field.path === selected;
        return (
          <span
            key={key}
            ref={isSelected ? selectedMark : undefined}
            className={`mark${isSelected ? " selected" : " needs"}`}
            style={toOverlay(box, page)}
            data-path={field.path}
            aria-hidden="true"
          />
        );
      })}
      {cited
        .filter((box) => box.page === page.number)
        .map((box, i) => (
          <span key={`cited-${i}`} className="mark selected cited" style={toOverlay(box, page)} aria-hidden="true" />
        ))}
    </figure>
  );
}
