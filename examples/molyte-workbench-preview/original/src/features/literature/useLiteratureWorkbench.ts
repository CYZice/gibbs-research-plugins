import { useCallback, useEffect, useRef, useState } from 'react';
import type { LiteratureWorkbenchProps } from './literatureWorkbenchContracts';

type SearchParamsUpdate =
  | URLSearchParams
  | ((current: URLSearchParams) => URLSearchParams);

export function useLiteratureWorkbench({
  initialSearch,
  onUrlStateChange,
}: Pick<LiteratureWorkbenchProps, 'initialSearch' | 'onUrlStateChange'>) {
  const initialSearchKey = initialSearch?.toString() || '';
  const [searchParams, setSearchParamsState] = useState(
    () => new URLSearchParams(initialSearchKey),
  );
  const searchParamsRef = useRef(searchParams);

  useEffect(() => {
    const next = new URLSearchParams(initialSearchKey);
    searchParamsRef.current = next;
    setSearchParamsState(next);
  }, [initialSearchKey]);

  const setSearchParams = useCallback((
    next: SearchParamsUpdate,
    options?: { replace?: boolean },
  ): void => {
    const current = new URLSearchParams(searchParamsRef.current);
    const resolved = typeof next === 'function' ? next(current) : next;
    const cloned = new URLSearchParams(resolved);
    searchParamsRef.current = cloned;
    setSearchParamsState(cloned);
    onUrlStateChange?.(new URLSearchParams(cloned), options);
  }, [onUrlStateChange]);

  const liveLiteratureWorkspaceParams = useCallback(
    () => new URLSearchParams(searchParamsRef.current),
    [],
  );

  return {
    searchParams,
    setSearchParams,
    liveLiteratureWorkspaceParams,
  };
}
