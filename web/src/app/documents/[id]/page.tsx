import Review from "@/components/Review";

export default async function DocumentPage(props: PageProps<"/documents/[id]">) {
  const { id } = await props.params;
  return <Review key={id} id={id} />;
}
