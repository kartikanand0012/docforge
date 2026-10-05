import KnowledgeBaseView from "@/components/KnowledgeBaseView";

export default async function KnowledgeBasePage(props: PageProps<"/collections/[id]">) {
  const { id } = await props.params;
  return <KnowledgeBaseView key={id} id={id} />;
}
