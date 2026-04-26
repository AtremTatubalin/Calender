import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.propdd.app',
  appName: 'PRO PDD',
  webDir: 'www',
  server: {
    // Замените на публичный HTTPS URL вашего Flask-приложения
    // Например: https://app.example.com
    url: 'https://REPLACE_WITH_YOUR_DOMAIN',
    cleartext: false,
  },
};

export default config;
