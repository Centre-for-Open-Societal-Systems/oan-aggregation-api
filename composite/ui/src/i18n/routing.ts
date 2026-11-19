import { defineRouting } from 'next-intl/routing';

// Add a locale here and a matching locales/<locale>.json to translate the console.
export const routing = defineRouting({
    locales: ['en'],
    defaultLocale: 'en'
});
