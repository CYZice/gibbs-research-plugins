const paths = {
  plus: "M12 4v16M4 12h16",
  minus: "M4 12h16",
  library: 'M4 4h6v16H4zM14 4l5-1 3 16-5 1z',
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4',
  studio: 'M4 4v16h16M7 14l4-5 4 3 5-7',
  dashboard: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  code: 'm8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18',
  guide: 'M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1m0-15c3-2 6-2 9-1v15c-3-1-6-1-9 1V5',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8m0-5v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4',
  login: 'M14 4h6v16h-6M3 12h12m-5-5 5 5-5 5',
  logout: 'M10 4H4v16h6M9 12h12m-5-5 5 5-5 5',
  refresh: 'M20 10a8 8 0 0 0-14-5L3 8m0-5v5h5M4 14a8 8 0 0 0 14 5l3-3m0 5v-5h-5',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 16v4h16v-4',
  search: 'M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15M16 16l5 5',
  next: 'm9 5 7 7-7 7',
  previous: 'm15 5-7 7 7 7',
  close: 'm6 6 12 12M6 18 18 6',
} as const;
export function Icon({ name }: { name: keyof typeof paths }) {
  return <svg className="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>;
}
