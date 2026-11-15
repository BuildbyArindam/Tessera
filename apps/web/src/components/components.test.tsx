import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RiskBadge } from "./InvoiceRisk";
import { WindowPicker } from "./Pickers";
import { Async } from "./States";

const never = () => false;

describe("Async", () => {
  it("shows a loading status while loading", () => {
    render(<Async remote={{ status: "loading" }} isEmpty={never} empty={<p>empty</p>}>{() => <p>data</p>}</Async>);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
  it("shows an alert with a working retry on error", () => {
    const retry = vi.fn();
    render(<Async remote={{ status: "error", message: "boom", retry }} isEmpty={never} empty={<p>empty</p>}>{() => <p>data</p>}</Async>);
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledOnce();
  });
  it("shows the empty state, not the children, when there is no data", () => {
    render(<Async remote={{ status: "ready", data: [] }} isEmpty={(d: unknown[]) => d.length === 0} empty={<p>empty</p>}>{() => <p>data</p>}</Async>);
    expect(screen.getByText("empty")).toBeInTheDocument();
    expect(screen.queryByText("data")).toBeNull();
  });
  it("renders the children when there is data", () => {
    render(<Async remote={{ status: "ready", data: [1] }} isEmpty={(d: number[]) => d.length === 0} empty={<p>empty</p>}>{(d) => <p>{d.length} rows</p>}</Async>);
    expect(screen.getByText("1 rows")).toBeInTheDocument();
  });
});

describe("RiskBadge", () => {
  const base = { already_late: false, model: "lgbm" as const, pay_date_low: null, pay_date_median: null, pay_date_high: null, scored_as_of: "2026-06-30" };
  it("never relies on colour alone: the level is in the text", () => {
    render(<RiskBadge risk={{ ...base, p_late: 0.72 }} />);
    expect(screen.getByText(/High risk · 72%/)).toBeInTheDocument();
  });
  it("says so when an invoice has no score", () => {
    render(<RiskBadge risk={null} />);
    expect(screen.getByText("Not scored")).toBeInTheDocument();
  });
  it("labels an overdue invoice as already late", () => {
    render(<RiskBadge risk={{ ...base, p_late: 1, already_late: true }} />);
    expect(screen.getByText("Already late")).toBeInTheDocument();
  });
});

describe("WindowPicker", () => {
  it("is a labelled radio group with the current window checked, and reports changes", () => {
    const onChange = vi.fn();
    render(<WindowPicker value={60} onChange={onChange} />);
    expect(screen.getByRole("radio", { name: "60 days" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "30 days" }));
    expect(onChange).toHaveBeenCalledWith(30);
  });
});
