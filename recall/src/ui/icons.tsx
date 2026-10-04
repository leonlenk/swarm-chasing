import type { ReactNode, SVGProps } from 'react';

type P = SVGProps<SVGSVGElement> & { size?: number };
const I = ({ size = 20, children, ...rest }: P & { children: ReactNode }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6}
    strokeLinecap="round" strokeLinejoin="round" aria-hidden {...rest}>{children}</svg>
);

export const Logo = (p: P) => (
  <I size={34} strokeWidth={1.4} {...p}>
    <path d="M9.2 4.2c2.7-1.6 6.4-.5 7.8 2.4 1.3 2.6.3 5.6-2.1 7.1" />
    <path d="M15.6 19.6c-2.8 1.4-6.3.2-7.6-2.7-1.2-2.6-.1-5.6 2.4-7" />
    <path d="M4.3 14.8C2.8 12 3.9 8.4 6.8 7.1c2.6-1.2 5.6-.1 7 2.4" />
    <path d="M19.7 9.3c1.4 2.8.2 6.3-2.7 7.6-2.6 1.2-5.6.1-7-2.4" />
  </I>
);
export const IHome = (p: P) => <I {...p}><path d="M3.5 10.5 12 4l8.5 6.5V20a1 1 0 0 1-1 1H15v-6h-6v6H4.5a1 1 0 0 1-1-1z" /></I>;
export const IBranch = (p: P) => <I {...p}><circle cx="6" cy="5.5" r="2" /><circle cx="6" cy="18.5" r="2" /><circle cx="18" cy="8" r="2" /><path d="M6 7.5v9M18 10c0 4-6 3.5-11 7" /></I>;
export const ITasks = (p: P) => <I {...p}><rect x="4.5" y="3.5" width="15" height="17" rx="2" /><path d="m8.5 12 2.2 2.2 4.8-4.8" /></I>;
export const IUsers = (p: P) => <I {...p}><circle cx="9" cy="8.5" r="3.2" /><path d="M3.5 19.5c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5" /><circle cx="16.8" cy="9" r="2.6" /><path d="M16 14.6c2.6 0 4.2 1.7 4.7 4.4" /></I>;
export const IShield = (p: P) => <I {...p}><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5v5.5M12 16.2v.3" /></I>;
export const IPulse = (p: P) => <I {...p}><circle cx="12" cy="12" r="8.5" /><path d="M6.5 12.5h2.6l1.6-4 2.6 8 1.6-4h2.6" /></I>;
export const IDoc = (p: P) => <I {...p}><path d="M7 3.5h6.5L18 8v11.5a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z" /><path d="M13.5 3.5V8H18M9 12.5h6M9 16h6" /></I>;
export const ISearch = (p: P) => <I {...p}><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" /></I>;
export const IPlay = (p: P) => <svg width={p.size ?? 18} height={p.size ?? 18} viewBox="0 0 24 24" aria-hidden><path d="M7 4.5v15l12.5-7.5z" fill="currentColor" /></svg>;
export const IPause = (p: P) => <svg width={p.size ?? 18} height={p.size ?? 18} viewBox="0 0 24 24" aria-hidden><rect x="6" y="4.5" width="4" height="15" rx="1" fill="currentColor" /><rect x="14" y="4.5" width="4" height="15" rx="1" fill="currentColor" /></svg>;
export const IChevron = (p: P) => <I size={18} {...p}><path d="m6.5 9.5 5.5 5.5 5.5-5.5" /></I>;
export const IChevronR = (p: P) => <I size={16} {...p}><path d="m9.5 6 6 6-6 6" /></I>;
export const IArrowR = (p: P) => <I size={16} {...p}><path d="M5 12h14M13.5 6.5 19 12l-5.5 5.5" /></I>;
export const IPrev = (p: P) => <I size={16} {...p}><path d="M6 5v14M18 5l-9 7 9 7z" fill="currentColor" /></I>;
export const INext = (p: P) => <I size={16} {...p}><path d="M18 5v14M6 5l9 7-9 7z" fill="currentColor" /></I>;
export const IClock = (p: P) => <I {...p}><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></I>;
export const IChat = (p: P) => <I {...p}><path d="M4.5 18.5 5.6 15A7.5 7.5 0 1 1 9 18.4z" /><path d="M9 11.5h.01M12 11.5h.01M15 11.5h.01" strokeWidth={2.2} /></I>;
export const IAlert = (p: P) => <I {...p}><path d="M12 4 21 19.5H3z" /><path d="M12 10v4.5M12 17v.2" /></I>;
export const IPie = (p: P) => <I {...p}><circle cx="12" cy="12" r="8.5" /><path d="M12 3.5V12h8.5" /><path d="M12 12 6 18" /></I>;
export const IDb = (p: P) => <I size={18} {...p}><ellipse cx="12" cy="6" rx="7" ry="2.5" /><path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6M5 12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5" /></I>;
export const IX = (p: P) => <I size={18} {...p}><path d="M6 6l12 12M18 6 6 18" /></I>;
export const ICheck = (p: P) => <I size={14} strokeWidth={2.4} {...p}><path d="m5 12.5 4.5 4.5L19 7.5" /></I>;
export const IExternal = (p: P) => <I size={14} {...p}><path d="M14 4.5h5.5V10M19.5 4.5 11 13M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" /></I>;
export const IFlag = (p: P) => <I size={16} {...p}><path d="M6 21V4.5M6 5h11l-2 4 2 4H6" /></I>;
export const IExport = (p: P) => <I {...p}><path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M5 14v5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5" /></I>;
export const IUpload = (p: P) => <I size={18} {...p}><path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M5 14v5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5" /></I>;
export const IFork = (p: P) => <I {...p}><circle cx="7" cy="5.5" r="2" /><circle cx="17" cy="5.5" r="2" /><circle cx="12" cy="18.5" r="2" /><path d="M7 7.5v1.5a3 3 0 0 0 3 3h4a3 3 0 0 0 3-3V7.5M12 12v4.5" /></I>;
export const ILive = (p: P) => <I {...p}><circle cx="12" cy="12" r="2.6" /><path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4M5 5a10 10 0 0 0 0 14M19 5a10 10 0 0 1 0 14" /></I>;
export const IMap = (p: P) => <I {...p}><path d="M3.5 6.5 9 4l6 2.5L20.5 4v13.5L15 20l-6-2.5-5.5 2.5z" /><path d="M9 4v13.5M15 6.5V20" /></I>;
export const IGrid = (p: P) => <I {...p}><rect x="4" y="4" width="6.5" height="6.5" rx="1.5" /><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5" /><rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5" /><path d="M13.5 16.8h6.5M16.8 13.5V20" /></I>;
export const IEye = (p: P) => <I {...p}><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" /><circle cx="12" cy="12" r="2.8" /></I>;
