// Every table Dataform reads but does not build must be declared here, or
// compilation fails. Two are declared as a worked example; add the rest as your
// migration needs them.
//
// Declare a table here ONLY if nothing under definitions/ builds it. A table that
// is both declared here and produced by an action will not compile.

declare({ schema: "raw", name: "payments" });
declare({ schema: "raw", name: "subscriptions" });
