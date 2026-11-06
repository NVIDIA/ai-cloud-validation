# Naming a run's resources

Read this when the sweeper and the cases have to agree, or when an NCP's
naming rules are tighter than the convention assumes.

## The convention, and what each part is for

Every resource a run creates carries a name built from three parts: the kit's
fixed prefix, a run id the runner substitutes, and a per-resource
discriminator. The same parts go into labels or tags wherever the API supports
them, because **a label survives what a truncated name cannot**.

    tfkit-{run_id}-<discriminator>

The convention is conservative rather than expressive: lowercase letters, digits
and hyphens, opening with a letter, inside 63 characters with room left for the
id. That is what survives every naming rule the four layers meet, and a name
the API truncates is one the sweeper cannot match back to its run.

**The three parts are fixed; the separator between them is not.** Some
identifiers forbid the hyphen -- a custom role id in one NCP takes letters, digits, `_`
and `.` and nothing else -- so a case there spells the same prefix and the same
run id with a separator the API accepts. What may not change is the prefix or
the id: both are what the sweeper matches on.

The run id is a label on a cloud resource, recorded in the result. Nothing is
persisted between invocations.

## The NCP's name, where an identifier is needed

A resource name may carry a hyphen; an identifier may not. The NCP's name
reaches this skill already converted, as the `terraform_type` prefix the module
plan declares — `ncp: my-isv` arrives as `my_isv_instance`. Going on to Go,
underscore and hyphen are both word boundaries and neither survives, so the
test function is `TestAccMyIsvInstance_lifecycle`. Either spelling arriving from
a plan renders the same identifier, so a plan that kept the hyphen does not
produce a second, uncompilable name.
