/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bi: {
          primary: 'var(--bi-color-primary)',
          'primary-hover': 'var(--bi-color-primary-hover)',
          'primary-dark': 'var(--bi-color-primary-dark)',
          accent: 'var(--bi-color-accent)',
          'accent-light': 'var(--bi-color-accent-light)',
          'bg-base': 'var(--bi-bg-base)',
          'bg-surface': 'var(--bi-bg-surface)',
          'bg-surface-light': 'var(--bi-bg-surface-light)',
          'bg-card': 'var(--bi-bg-card)',
          'bg-card-hover': 'var(--bi-bg-card-hover)',
          'border-subtle': 'var(--bi-border-subtle)',
          'border-default': 'var(--bi-border-default)',
          'border-emphasis': 'var(--bi-border-emphasis)',
          'text-primary': 'var(--bi-text-primary)',
          'text-secondary': 'var(--bi-text-secondary)',
          'text-muted': 'var(--bi-text-muted)',
          'text-dimmed': 'var(--bi-text-dimmed)',
          'status-ok': 'var(--bi-status-ok)',
          'status-warn': 'var(--bi-status-warn)',
          'status-danger': 'var(--bi-status-danger)',
        },
      },
      borderRadius: {
        bi: 'var(--bi-radius-lg)',
        'bi-sm': 'var(--bi-radius-sm)',
        'bi-md': 'var(--bi-radius-md)',
        'bi-xl': 'var(--bi-radius-xl)',
      },
      fontFamily: {
        bi: ['Inter', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
