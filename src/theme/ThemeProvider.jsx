import { useEffect } from 'react';
import theme from '../config/theme.json';

export default function ThemeProvider({ children }) {
  useEffect(() => {
    const root = document.documentElement;

    const vars = {
      '--bi-color-primary': theme.colors.primary,
      '--bi-color-primary-hover': theme.colors.primaryHover,
      '--bi-color-primary-dark': theme.colors.primaryDark,
      '--bi-color-accent': theme.colors.accent,
      '--bi-color-accent-light': theme.colors.accentLight,
      '--bi-bg-base': theme.colors.bg.base,
      '--bi-bg-surface': theme.colors.bg.surface,
      '--bi-bg-surface-light': theme.colors.bg.surfaceLight,
      '--bi-bg-card': theme.colors.bg.card,
      '--bi-bg-card-hover': theme.colors.bg.cardHover,
      '--bi-border-subtle': theme.colors.border.subtle,
      '--bi-border-default': theme.colors.border.default,
      '--bi-border-emphasis': theme.colors.border.emphasis,
      '--bi-text-primary': theme.colors.text.primary,
      '--bi-text-secondary': theme.colors.text.secondary,
      '--bi-text-muted': theme.colors.text.muted,
      '--bi-text-dimmed': theme.colors.text.dimmed,
      '--bi-status-ok': theme.colors.status.ok,
      '--bi-status-warn': theme.colors.status.warn,
      '--bi-status-danger': theme.colors.status.danger,
      '--bi-gradient-bg': theme.gradients.background,
      '--bi-gradient-bg-dashboard': theme.gradients.backgroundDashboard,
      '--bi-gradient-primary-btn': theme.gradients.primaryButton,
      '--bi-gradient-text': theme.gradients.text,
      '--bi-font-family': theme.fonts.family,
      '--bi-radius-sm': theme.borderRadius.sm,
      '--bi-radius-md': theme.borderRadius.md,
      '--bi-radius-lg': theme.borderRadius.lg,
      '--bi-radius-xl': theme.borderRadius.xl,
      '--bi-radius-full': theme.borderRadius.full,
    };

    for (const [prop, value] of Object.entries(vars)) {
      root.style.setProperty(prop, value);
    }

    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = theme.fonts.importUrl;
    document.head.appendChild(link);

    return () => {
      document.head.removeChild(link);
    };
  }, []);

  return children;
}

export { theme };
