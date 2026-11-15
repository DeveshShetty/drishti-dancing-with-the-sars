/* DRISHTI viewer settings. Copy this file to viewer/config.js (which is never committed) and fill in.

   googleMapsKey: optional Google Maps JavaScript API key. When set, a "Google" style appears in the Basemap panel;
   without it the viewer works fully with the Esri, Topo, Streets and 3D basemaps.
   A browser key is visible to visitors, so restrict it in Google Cloud Console:
     APIs & Services > Credentials > your key
       Application restrictions: Websites -> http://localhost:8000/* and https://YOUR-DOMAIN/*
       API restrictions: Maps JavaScript API
*/
window.NCE_CONFIG = {
  googleMapsKey: ""
};
