export interface BrandMarkProps {
  className?: string;
}

export function BrandMark({ className }: BrandMarkProps) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      focusable="false"
      viewBox="0 0 32 32"
      xmlns="http://www.w3.org/2000/svg"
    >
      <line x1="13" x2="13" y1="3.5" y2="28.5" stroke="currentColor" strokeWidth="1.5" />
      <line x1="19" x2="19" y1="3.5" y2="28.5" stroke="currentColor" strokeWidth="1.5" />
      <path
        d="M16 5.75c6.15 0 10.5 4.08 10.5 10.2 0 6.17-4.43 10.3-10.5 10.3S5.5 22.12 5.5 15.95C5.5 9.83 9.85 5.75 16 5.75Z"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="2.35"
      />
      <path
        d="m8.15 9.35 4.85 6.6 6-6.25 4.85 6.25L19 22.3l-6-6.35-4.85 6.7"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.35"
      />
    </svg>
  );
}
