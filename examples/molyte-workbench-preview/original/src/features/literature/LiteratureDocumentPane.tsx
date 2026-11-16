import type { ReactNode } from 'react';

export default function LiteratureDocumentPane({
  children,
}: {
  children: ReactNode;
}) {
  return <main className="literature-reader">{children}</main>;
}
