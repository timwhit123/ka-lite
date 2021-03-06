import glob
import time
from annoying.functions import get_object_or_None
from optparse import make_option

from django.core.management.base import BaseCommand, CommandError

import settings
from main.models import VideoFile
from shared import caching
from utils.general import break_into_chunks
from utils.videos import download_video


class Command(BaseCommand):
    help = "Sync up the database's version of what videos have been downloaded with the actual folder contents"

    option_list = BaseCommand.option_list + (
        make_option('-c', '--cache',
            action='store_true',
            dest='auto_cache',
            default=False,
            help='Create cached files',
            metavar="AUTO_CACHE"),
    )

    def handle(self, *args, **options):
        caching_enabled = (settings.CACHE_TIME != 0)
        touched_video_ids = []

        # delete VideoFile objects that are not marked as in progress, but are neither 0% nor 100% done; they're broken
        video_files_to_delete = VideoFile.objects.filter(download_in_progress=False, percent_complete__gt=0, percent_complete__lt=100)
        youtube_ids_to_delete = [d["youtube_id"] for d in video_files_to_delete.values("youtube_id")]
        video_files_to_delete.delete()

        if caching_enabled:
            for youtube_id in youtube_ids_to_delete:
                caching.invalidate_all_pages_related_to_video(video_id=youtube_id)
                touched_video_ids.append(youtube_id)
        if len(video_files_to_delete):
            self.stdout.write("Deleted %d VideoFile models (to mark them as not downloaded, since they were in a bad state)\n" % len(video_files_to_delete))

        files = glob.glob(settings.CONTENT_ROOT + "*.mp4")
        subtitle_files = glob.glob(settings.CONTENT_ROOT + "*.srt")
        videos_marked_at_all = set([video.youtube_id for video in VideoFile.objects.all()])
        videos_marked_as_in_progress = set([video.youtube_id for video in VideoFile.objects.filter(download_in_progress=True)])
        videos_marked_as_unstarted = set([video.youtube_id for video in VideoFile.objects.filter(percent_complete=0, download_in_progress=False)])
        
        videos_in_filesystem = set([path.replace("\\", "/").split("/")[-1].split(".")[0] for path in files])
        videos_in_filesystem_chunked = break_into_chunks(videos_in_filesystem)

        videos_flagged_for_download = set([video.youtube_id for video in VideoFile.objects.filter(flagged_for_download=True)])

        subtitles_in_filesystem = set([path.replace("\\", "/").split("/")[-1].split(".")[0] for path in subtitle_files])
        subtitles_in_filesystem_chunked = break_into_chunks(subtitles_in_filesystem)
        
        count = 0
        for chunk in videos_in_filesystem_chunked:
            video_files_needing_model_update = VideoFile.objects.filter(percent_complete=0, download_in_progress=False, youtube_id__in=chunk)
            count += video_files_needing_model_update.count()
            video_files_needing_model_update.update(percent_complete=100, flagged_for_download=False)
            if caching_enabled:
                for vf in video_files_needing_model_update:
                    caching.invalidate_all_pages_related_to_video(video_id=vf.youtube_id)
                    touched_video_ids.append(vf.youtube_id)
        if count:
            self.stdout.write("Updated %d VideoFile models (to mark them as complete, since the files exist)\n" % count)
        
        video_ids_needing_model_creation = list(videos_in_filesystem - videos_marked_at_all)
        count = len(video_ids_needing_model_creation)
        if count:
            VideoFile.objects.bulk_create([VideoFile(youtube_id=youtube_id, percent_complete=100) for youtube_id in video_ids_needing_model_creation])
            if caching_enabled:
                for vid in video_ids_needing_model_creation:
                    caching.invalidate_all_pages_related_to_video(video_id=vid)
                    touched_video_ids.append(vid)
            self.stdout.write("Created %d VideoFile models (to mark them as complete, since the files exist)\n" % count)
        
        count = 0
        videos_needing_model_deletion_chunked = break_into_chunks(videos_marked_at_all - videos_in_filesystem - videos_flagged_for_download)
        for chunk in videos_needing_model_deletion_chunked:
            video_files_needing_model_deletion = VideoFile.objects.filter(youtube_id__in=chunk)
            count += video_files_needing_model_deletion.count()
            video_files_needing_model_deletion.delete()
            if caching_enabled:
                for video_id in chunk:
                    caching.invalidate_all_pages_related_to_video(video_id=video_id)
                    touched_video_ids.append(video_id)
        if count:
            self.stdout.write("Deleted %d VideoFile models (because the videos didn't exist in the filesystem)\n" % count)

        count = 0
        for chunk in subtitles_in_filesystem_chunked:
            video_files_needing_model_update = VideoFile.objects.filter(subtitle_download_in_progress=False, subtitles_downloaded=False, youtube_id__in=chunk)
            count += video_files_needing_model_update.count()
            video_files_needing_model_update.update(subtitles_downloaded=True)
            if caching_enabled:
                for vf in video_files_needing_model_update:
                    caching.invalidate_all_pages_related_to_video(video_id=vf.youtube_id)
                    touched_video_ids.append(vf.youtube_id)
                    
        if count:
            self.stdout.write("Updated %d VideoFile models (marked them as having subtitles)\n" % count)

        if options["auto_cache"] and caching_enabled and touched_video_ids:
            caching.regenerate_all_pages_related_to_videos(video_ids=touched_video_ids)
