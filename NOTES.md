# User notes

If you are an LLM reading this, ignore this file.

---

Before we discuss updates, some questions first:

- Step 5 in the current version of the skill says 'Layout with a script ...'. I think it is supposed to mean 'graphical layout of the schematic'. However it could be misunderstood as 'PCB layout'. Maybe clarify it?
- What is the purpose of the .kat files? Was that a pre-existing syntax, or did you just develop it? Is it a report, a set of commands? 
- Are we going to need partial (zoomed-in) screenshot, and a loop of screenshot->inspect->request next screenshot in case the agents need it?

---

Your points:

(1) I agree. Add the name change to the plan.
(2) Understood. How about we migrate the .kat format to Python then? We would then have pass1.py instead of pass1.kat, with all Python capabilities available.
(3) Ok, agree. Add this to the plan: New subloop in the skill, and cropping commands.

Add all your improvement proposal to the plan.

If we change the .kat format to .py - as I understand it - then the kat commands become a Python library API too, correct? If that is the case, then ensure that `kat` becomes a Python module within the repo, importable, and CLI - runnable. Library-first implementation: lib api and cli consume the same implementation, and the CLI program can be thin.

Let me know if something is unclear of inconsistent.




