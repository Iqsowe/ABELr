
## ABELr
Automated Batch Editing for Lightroom\
This project is an Adobe Lightroom Classic Plugin made to help batch editing pain.

## Back story
I've started photography in August 2024, shooting at convention with cosplayers.\
I quickly started to shoot everything and everyone with the mighty power of my Sony camera.\
Everyone looked so enlighted by the pictures I made I couldn't stop.

Now it's July 2026, I shoot likely 1500 photos per event (almost no burst, always single pics), and end up with roughtly 5000 photos per month.\
As much as I love giving the best result, I can't do it anymore (I did in the past, spending 50h in 10 days instead of sleeping, over and over).

This project is the fifth (sitxh ?) iteration of trying the make Adobe Lightroom better for my needs.\
I've considered Darktable, ART rawtherapee, Rapid RAW, even doing my own RAW Editor, but Lightroom has it's signature process making hard to leave it.\
I've spent months trying to do something, battle with Github Copilot and now Claude Code (yes, we'll speak about that later).\
And this might be the closest project to succeed the task.

## The project
The project consist of :
- A plugin "ABELr" serving as a bridge between the Python core and every functionnalities, being and API and likely an MCP for Claude.
- A python server with a GUI and simple buttons like "Test", "Analyse Catalog", "Mark references", "Apply"
- A whole processing and calculation part I don't know at all because despite being a developer, I don't know anything about GPU Image processing, Luminance median, Gray world nor HSL calculations, so I let AI do it's magic while trying to supervise the work

The plugin folder contain all the python application, you just need to add the folder in File->External Module Manager.\
To launch the app server, in the Plugin Manager, click on ~~"Démarrer l'Application" (sorry I'm French I haven't translated the project yet)~~ "Start / Connect"

Be sure to install Python 3.1x and the virtual env needed (I just don't know why pytorch takes 4GB that's insane).\
I use GPU processing because CPU usage just froze my PC to death during image analysis, I'm trying to maintain the CPU fallback, feel free to help if you need it.

## Installation
Plugin installation :
- Download and install Python on your system (3.12 for example)
- Download the project
- Open Adobe Lightroom Classic, and go into File->External Modules Manager (or Plugin Manager)
- Bottom Left, click on "Add", find and select the folder "ABELr.lrplugin"
- Activate the plugin "ABELr" and click on "reload the external module", it should give a green dot if correctly added
- Top menu of the plugin, click on "Start / Connect"
- On the first launch, the python virtual env will install itself, give it some time. The script are in Powershell, I'll look to add shell script for MAC/Linux
- When finished, you might need to close the application, and restart Lightroom (or just reload the plugin, to verify)
- Relaunch the app with "Start / Connect", the GUI window displays and a popup message will appear in Lightroom, meaning the plugin is activated

Now you can use the app functionalities.\
If you use Claude Code, the API is accessible through http://127.0.0.1:5000, I haven't described it yet.\
For Claude, add the project folder as a reference path, it will find how to use it, like an MCP.

## Functionalities and how it's works
Basic Context : Open Adobe Lightroom Classic, Open or Start a Catalog, get your photo displayed.\
Let's say you have 1000 photos (my daily punishment), apply the preset you want on your photos (the signature look of your set)

Start ABELr, the plugin will maintain communication on the given port, and the app will request it for every action.

Start with "Analyse Catalog", it will create a db file in the active catalog folder. This db file will contain everything needed for analysis and calculation.\
Edit a group of photos the way you want them (tone curve, color grading, effect, corrections, masking)

Select them and start "Mark references" : The app will process the RAW source files with CUDA, analysing luminance, chrominance, and sharp zone.\
The RAW results are saved in the db file. It will do the same for the Preview images in LrPreview.lrdata, and get all develop settings.\
The selected photos are now reference seeds and will help to determine how to properly set Exposure, White-Balance, HSL cursors and Calibration cursors on the next photos.

Now select the target photos to correct, start "Calibrate neutral" : the app will set Exposure 0 and WB As shot, and analyse the Previews, then restore the settings.\
Start "Apply" on the selected photos : The app will calculate global and sharp luminance/chrominance/HSL and use seeds to find the closest matching photo to use.

When edits are applied, you can adjust them one by one (Exposure, WB, HSL and Calibration adjustments), then launch "Mark references" to add them to the seeds pool.\

Keep doing this on all the photos, the more seeds you have the better the prediction get.\
And voila, you have correctly aligned the whole event on your preset.

## Known limitations
This project was (very) personal, and I'm sharing it with you thinking it might serve a much bigger purpose, and help many other people struggling with the same problem.\
So I'm asking for help, I would like help to make the best tool it can be, and solve the things I can't do alone.

Actual context limitations :
- I'm on Windows 11, scripts are in powershell and other python packages run for Windows. If you have a Mac or Linux, feel free to add support for your hardware.
- I use a GTX 2080 GPU, all image processing needs GPU, I've normally added CPU only support. You can review this part if you need it.
- I use a Sony A7 IV Camera, exif data and raw processing works for .ARW files. It can be tricky on some part, but I'm kindly asking for other camera support.
- The app isn't finished, some functionnalities doesn't completely give the wanted result. I can battle Claude all I want, I still miss Photography knowledge for this.

## Tips
I always generate all photos Previews (in Library Module, top right under the Histogram, you can click on "Dynamic previews" to generate them).\
I've recently found that using Adobe Profile (DCP) with the same Camera Matching profile than your In Camera JPEG makes Adobe Lightroom Auto function more effective.\
My workflow consist of :
- Create 1 or 2 presets for the Catalog (Adobe Profile, Effects, Tone Curve, Color Grading, Lens Correction, Dezoising, Masks etc)
- Apply the preset on my whole Catalog
- Click Auto to set Automatic Tone settings on every photos
- Apply again my preset (if I use fixed Vibrance/Saturation or specific tones like Highlights/Shadows)
- Pass on each photos to adjust Exposure, White Balance, Calibration cursors

With this plugin the goal is to do it the least possible, and apply corrections matching how I want it for this serie.

## AI
Yes, the whole project development is done with Claude Code.\
I know nothing about CUDA, rawpy, and nothing about image engineering.\
Claude started it, I let him doing whatever he need, while trying to enclose it the best I can.

If you know better how to make CLAUDE.MD, ARCHITECTURE.MD or PLAN.MD, be my guest.\
If you know how luminance and gray world works, and you have a better solution for some functions, feel free to make suggestions.

For now, the little knowledge and time I have, Claude is my best coworker to make it happens.

## Closure
That's pretty much all, I've certainly skipped many things, but that's a start.\
Feel free to ask questions on Github, do pull requests, contribute to the project.\
I'm certainly wrong on sooo many things (the program, my workflow, how I use Claude).\
But I have so many hopes people would kindly help if the project is interesting enough.

So thank you, if anything on this project has lit something in you, and you decided to use, contribute or help the project.

##
PS : I'm not a professional photographer, I do all this (photos, editing, research) for free, with passion for the people I meet, and the humanity I'm glad to be in.