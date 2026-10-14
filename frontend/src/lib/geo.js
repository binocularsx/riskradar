/**
 * Nigerian states for the location views (D109).
 *
 * `transactions.ip_region` is an ISO 3166-2 code for the state the session's IP
 * address resolved to. Centroids here are approximate, in degrees, for the
 * Performance reports dot map. The outline maps (StateMap) use the real state
 * boundaries in nigeriaStates.js instead.
 */

import { SHAPES } from './nigeriaStates'

// `label`: where the name goes when the default, above the circle, would sit
// on a neighbouring state.
export const STATES = {
  'NG-LA': { name: 'Lagos', lat: 6.52, lon: 3.38, label: 'below' },
  'NG-OY': { name: 'Oyo', lat: 8.15, lon: 3.60 },
  'NG-KD': { name: 'Kaduna', lat: 10.52, lon: 7.44 },
  'NG-KN': { name: 'Kano', lat: 11.75, lon: 8.52 },
  'NG-EN': { name: 'Enugu', lat: 6.45, lon: 7.50 },
  'NG-AB': { name: 'Abia', lat: 5.45, lon: 7.52, label: 'right' },
  'NG-RI': { name: 'Rivers', lat: 4.85, lon: 6.95, label: 'below' },
  'NG-DE': { name: 'Delta', lat: 5.70, lon: 5.93, label: 'below' },
  'NG-FC': { name: 'FCT Abuja', lat: 9.07, lon: 7.40 },
}

// Every other state's name comes from the boundary file.
export const stateName = (code) => STATES[code]?.name
  || (code === 'NG-FC' ? 'FCT Abuja' : SHAPES[code]?.name) || code || 'Unknown'

// Nigeria's bounding box.
export const BOUNDS = { west: 2.6, east: 14.7, south: 4.0, north: 13.9 }

// The part of the country the data covers. Every located state sits south and
// west of Kano, so a whole-country frame leaves the north-east empty and packs
// Lagos, Oyo, Delta, Abia and Rivers into a corner where their labels collide.
export const DATA_BOUNDS = { west: 2.4, east: 10.2, south: 4.2, north: 12.5 }

/** A projection for a W×H box with margin M, so edge states keep their symbols. */
export function projection(W, H, M, b = BOUNDS) {
  return {
    x: (lon) => M + ((lon - b.west) / (b.east - b.west)) * (W - 2 * M),
    y: (lat) => (H - M) - ((lat - b.south) / (b.north - b.south)) * (H - 2 * M),
  }
}
