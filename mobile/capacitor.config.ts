import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'sh.serena.app',
  appName: 'Serena',
  webDir: 'dist',
  // Serve the app from an INSECURE origin (http://localhost) so a plain ws://
  // to the daemon isn't blocked as mixed content. Blink blocks insecure ws://
  // from an https:// origin before any packet — allowMixedContent does NOT
  // cover WebSockets. Pair with android:usesCleartextTraffic="true".
  server: { androidScheme: 'http' },
  android: { allowMixedContent: true },
  // For live-reload dev against a running daemon, set the URL here (PC step):
  //   server: { url: 'http://100.x.x.x:PORT', cleartext: true }
};

export default config;
