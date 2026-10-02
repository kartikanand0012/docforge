"use client";

import type { FieldAssessment } from "@/lib/api";
import { toOverlay } from "@/lib/geometry";

type Props = {
  src: string;
  page: { number: number; width: number; height: number };
  fields: FieldAssessment[];
  selected: string | null;
};

/** One page of the original as uploaded, with the selected value's source outlined. */
export default function PageView({ src, page, fields, selected }: Props) {
  const marks = fields.flatMap((field) =>
    field.boxes
      .filter((box) => box.page === page.number)
      .filter(() => field.path === selected || field.needs_review)
      .map((box, i) => ({ key: `${field.path}-${i}`, box, field })),
  );
  return (
    <figure className="page" style={{ margin: 0, marginBottom: 12 }}>
      {/* eslint-disable-next-line @next/next/no-img-element -- served by the API, sized by the page */}
      <img src={src} alt={`Page ${page.number} of the original document`} width={page.width} height={page.height} />
      {marks.map(({ key, box, field }) => (
        <span
          key={key}
          className={`mark${field.path === selected ? " selected" : field.needs_review ? " needs" : ""}`}
          style={toOverlay(box, page)}
          data-path={field.path}
          aria-hidden="true"
        />
      ))}
    </figure>
  );
}
