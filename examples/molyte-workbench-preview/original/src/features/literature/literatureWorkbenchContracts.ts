import type { LiteratureDiagnosticHandoff } from '../../api/literatureWorkspace';

export interface LiteratureWorkbenchProps {
  mode: 'standalone' | 'inspector';
  targetSessionId?: string;
  targetProjectId: number | null;
  initialSearch?: URLSearchParams;
  onUrlStateChange?: (
    search: URLSearchParams,
    options?: { replace?: boolean },
  ) => void;
  onHandoffComplete: (result: LiteratureDiagnosticHandoff) => void;
}
