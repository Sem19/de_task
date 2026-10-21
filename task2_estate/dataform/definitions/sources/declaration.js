// Every table Dataform reads but does not build must be declared here, or
// compilation fails. Two are declared as a worked example; add the rest as your
// migration needs them.
//
// Declare a table here ONLY if nothing under definitions/ builds it. A table that
// is both declared here and produced by an action will not compile.
//
// dataformCoreVersion 3.0.0 accepts a tags field on declare() and then drops it:
// the compiled declaration has no tags. The revenue_daily tag lives on the
// staging tables that read these sources.

declare({
  database: "acme-analytics-prod",
  schema: "raw",
  name: "payments",
});
declare({
  database: "acme-analytics-prod",
  schema: "raw",
  name: "subscriptions",
});
declare({
  database: "acme-analytics-prod",
  schema: "raw",
  name: "fx_rates",
});
