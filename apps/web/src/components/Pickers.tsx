"use client";

import { useId } from "react";
import type { Business } from "@/api/client";
import { WINDOWS, type Window } from "@/lib/forecast";

export function WindowPicker({ value, onChange }: { value: Window; onChange: (w: Window) => void }) {
  const name = useId();
  return (
    <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
      <legend className="sr-only">Forecast window</legend>
      <div className="seg">
        {WINDOWS.map((w) => (
          <label key={w}>
            <input type="radio" name={name} value={w} checked={value === w} onChange={() => onChange(w)} />
            {w} days
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export function BusinessPicker({ businesses, value, onChange }: { businesses: Business[]; value: string; onChange: (id: string) => void }) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="sr-only">
        Business
      </label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        {businesses.map((b) => (
          <option key={b.id} value={b.id}>
            {b.name}
          </option>
        ))}
      </select>
    </div>
  );
}
