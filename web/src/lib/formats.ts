/** What the person uploaded, in words. */
const NAMES: Record<string, string> = {
  "application/pdf": "PDF",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "Word",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PowerPoint",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "Excel",
  "image/png": "PNG image",
  "image/jpeg": "JPEG image",
  "image/tiff": "TIFF image",
};

export const formatName = (mediaType: string): string => NAMES[mediaType] ?? mediaType;

/** For the file picker. The server decides from the bytes, whatever this lets through. */
export const ACCEPT = [".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ...Object.keys(NAMES)].join(",");
