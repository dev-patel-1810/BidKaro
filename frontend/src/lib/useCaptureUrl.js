import { useEffect, useState } from "react";

import { api } from "./api";

/**
 * Load an authenticated capture as an object URL.
 *
 * A challenge shows two pictures: the captcha itself and the full page it was
 * taken from. Both live behind the authenticated capture route, so they are
 * fetched as blobs and released when the picture changes.
 */
export function useCaptureUrl(path) {
  const [url, setUrl] = useState(null);

  useEffect(() => {
    if (!path) {
      setUrl(null);
      return undefined;
    }
    let cancelled = false;
    let objectUrl = null;
    api
      .getBlob(path)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setUrl(null);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  return url;
}

/** Strip the `/api` prefix the browser route adds, for `api.getBlob`. */
export function capturePath(challenge, key = "image_url") {
  const value =
    key === "screenshot_url"
      ? challenge?.context?.screenshot_url
      : challenge?.image_url;
  return value ? value.replace(/^\/api(?=\/)/, "") : null;
}
