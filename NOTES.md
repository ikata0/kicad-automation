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

---

Now let us continue with the layouting part, which shall happen once the schematic is in a state ready for the first layout iteration. This seems to be far better covered by kicad-python, correct? 
What would be a reasonable workflow?
I am thinking of
1. For each component in the schematic, select a footprint and decide for one or more specific parts matching the specification.
2. Ensure we have parts library entries for each.
3. Create a new layout.
4. PCB shape. First estimate of the number of layers needed.
5. Component placement according to design rules and spec.
    Are there functional blocks?
    Are there repetitive blocks that might better be layouted once and then multiplied?
6. Routing. How? Full auto-routing? 
    Or iterative cycles of 
        6.1 LLM partially auto-routes with constraints,
        6.2 feedback via routing results and screenshot,
        6.3 correct, back to 6.1.

7. Fiducial marks, text prints, etc. 
8. DRC and go to 5 or 6.


Would you recommend two separate skills (schematic / layout) that could be parts of a top-level kicad skill? Or rather a single skill that would account better for cases where, while layouting, the agent becomes aware that the schematic must be changed again?

---

Some comments:
1. I think we should not rely on the deprecated pcbnew unless there is no other way.
2. Just looked up kicad online: 10.0.6 seems to be the latest version already. 
3. Please elaborate: What does 'building the board from the schematic' mean?
4. Explain 'parity'.
5. If 'building board from schematic' is necessary - any other way than old pcbnew? Seems like an important step that kicad-python does not offer?
6. So we are going to operate in some kind of GUI-alternating mode - like this?
    - GUI off. Board from schematic via kat->pcbnew,
    - GUI off. Parts placement, board, GND pour, keep-out via kat->pcbnew,
    - GUI on. Basic structure & constraints by agent via kicad-python, 
    - GUI off. Autorouting.
    - GUI off. Checks, pics, fabrication via kicad-cli.



---

DONE

## Questions before we continue

1. I think both the e2e tests for `kat` and the skill should, as the layout matures, try opening it in kicad, to see if kicad accepts the files. What is your opinion about this and is it covered?

2. I am reading the pcb layout skill text myself. What does '3. Place in a Python pass" mean? Which commands are used for that? The first Python recipe looks like it showcases how to do placing in one Python pass.

3. Step 2 (Setup) is designed to be performed by a single python script, correct? I see some of its features in your first recipe.

4. What does fanout mean in the PCB layout context?

5. Are keep-outs and pours covered in the kat.pcb library? How is an agent going to look up the usage of the commands?


---

DONE

## Features 

### Journal of the development process

During and after a PCBA design, I would like to be able to review what the agent did, step-by-step.
We should have journaling functionality in the `kat` module.
What I am thinking of:
- When a project is started, `kat` ensure that there is a file `<PROJECT_NAME>_REPORT.md` in the project root folder (create if not existing).
- When a kat CLI command is run, a new entry with the command syntax is made in the .md, followed by the results of it.
- When a batch Python file is executed I would prefer to put the actual, original code (not some long list of signals that came from a loop) into the report. With some `python FILENAME.py` command, automatic journaling would probably not work.
Can we have a command

    `kat batch-exec FILENAME.py`

that will execute the file, but also append a code block to the report .md ?

- All report entries with running index, date, time.
- Add another command, both in CLI and library API, which the agent can use to add a markdown comment to the report file. 
- Comment entries can tell what the agent plans to do next, summarize milestones reached, of describe errors that need iteration of the design.
- Snapshots get a report entry too, with both the snapshot command that was called, and the result images. (Link the images, put them into a sub-folder next to the report file)

### Opening the result in KiCAD

I agree with your proposals, please add it to the plan.

### Recipes in the skill description

Agree, please split the recipe into a setup and a placement part.

### Setup code

Yes, make it safe to re-run.

### Documentation for agents

Yes, proceed with your kat api proposal, I would even generalize it to `kat api` instead of `kat pcb api`.
I assume that `kat api kat.MODULE1.function2` will then be equivalent to 

    python -c "import kat; help(kat.MODULE1.function2)

Correct?

---



