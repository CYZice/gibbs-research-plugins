import type { ReactNode } from 'react';

export default function LiteratureLibraryPane({
  children,
}: {
  children: ReactNode;
}) {
  return <aside className="literature-results">{children}</aside>;
}
