import DocumentView from "@/components/DocumentView";

export default async function DocumentPage(props: PageProps<"/documents/[id]">) {
  const { id } = await props.params;
  return <DocumentView key={id} id={id} />;
}
