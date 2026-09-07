using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using XUnity.Common.Utilities;

namespace XUnity.AutoTranslator.Plugin.Core.UI
{
   internal class TranslationAggregatorOptionsWindow
   {
      private static bool _isScrollViewSupported = true;
      private const int WindowId = 45733721;
      private const float WindowWidth = 320;

      private Rect _windowRect = new Rect( 20, 20, WindowWidth, 400 );
      private bool _isMouseDownOnWindow = false;
      private TranslationAggregatorViewModel _viewModel;
      private List<ToggleViewModel> _toggles;
      private Vector2 _scrollPosition;

      public TranslationAggregatorOptionsWindow( TranslationAggregatorViewModel viewModel )
      {
         _viewModel = viewModel;
         _toggles = _viewModel.AllTranslators.Select( x =>
         new ToggleViewModel(
            " " + x.Endpoint.Endpoint.FriendlyName,
            null,
            null,
            () => x.IsEnabled = !x.IsEnabled,
            () => x.IsEnabled,
            x.Endpoint.Error == null ) ).ToList();
      }

      public bool IsShown
      {
         get => _viewModel.IsShowingOptions;
         set => _viewModel.IsShowingOptions = value;
      }

      public void OnGUI()
      {
         GUI.Box( _windowRect, GUIUtil.none, GUIUtil.GetWindowBackgroundStyle() );

         _windowRect = GUI.Window( WindowId, _windowRect, (GUI.WindowFunction)CreateWindowUI, "---- Translation Aggregator Options ----" );

         if( GUIUtil.IsAnyMouseButtonOrScrollWheelDownSafe )
         {
            var point = new Vector2( UnityInput.Current.mousePosition.x, Screen.height - UnityInput.Current.mousePosition.y );
            _isMouseDownOnWindow = _windowRect.Contains( point );
         }

         if( !_isMouseDownOnWindow || !GUIUtil.IsAnyMouseButtonOrScrollWheelSafe )
            return;

         // make sure window is focused if scroll wheel is used to indicate we consumed that event
         GUI.FocusWindow( WindowId );

         var point1 = new Vector2( UnityInput.Current.mousePosition.x, Screen.height - UnityInput.Current.mousePosition.y );
         if( !_windowRect.Contains( point1 ) )
            return;

         UnityInput.Current.ResetInputAxes();
      }

      private void CreateWindowUI( int id )
      {
         try
         {
            AutoTranslationPlugin.Current.DisableAutoTranslator();

            if( GUI.Button( GUIUtil.R( WindowWidth - 22, 2, 20, 16 ), "X" ) )
            {
               IsShown = false;
            }

            GUILayout.Label( "Available Translators", ArrayHelper.Null<GUILayoutOption>() );

            // GROUP
            if( _isScrollViewSupported )
            {
               bool hasStartedScrollView = false;
               try
               {
                  _scrollPosition = GUILayout.BeginScrollView( _scrollPosition, GUI.skin.box, ArrayHelper.Null<GUILayoutOption>() );
                  hasStartedScrollView = true;

                  foreach( var vm in _toggles )
                  {
                     var previousEnabled = GUI.enabled;

                     GUI.enabled = vm.Enabled;
                     var previousValue = vm.IsToggled();
                     var newValue = GUILayout.Toggle( previousValue, vm.Text, ArrayHelper.Null<GUILayoutOption>() );
                     if( previousValue != newValue )
                     {
                        vm.OnToggled();
                     }

                     GUI.enabled = previousEnabled;
                  }
               }
               catch( System.Exception e )
               {
                  if ( e is System.NotSupportedException )
                  {
                     XUnity.Common.Logging.XuaLogger.AutoTranslator.Warn( e, "An error occurred while calling GUILayout.BeginScrollView. Fallback mode will be used." );
                     _isScrollViewSupported = false;
                  }
                  else throw;
               }
               finally
               {
                  if( hasStartedScrollView ) GUILayout.EndScrollView();
               }
            }

            if( !_isScrollViewSupported )
            {
               GUILayout.BeginVertical( GUI.skin.box, ArrayHelper.Null<GUILayoutOption>() );

               foreach( var vm in _toggles )
               {
                  var previousEnabled = GUI.enabled;

                  GUI.enabled = vm.Enabled;
                  var previousValue = vm.IsToggled();
                  var newValue = GUILayout.Toggle( previousValue, vm.Text, ArrayHelper.Null<GUILayoutOption>() );
                  if( previousValue != newValue )
                  {
                     vm.OnToggled();
                  }

                  GUI.enabled = previousEnabled;
               }

               GUILayout.EndVertical();
            }

            GUILayout.BeginHorizontal( ArrayHelper.Null<GUILayoutOption>() );
            GUILayout.Label( "Height", ArrayHelper.Null<GUILayoutOption>() );
            _viewModel.Height = Mathf.Round( GUILayout.HorizontalSlider( _viewModel.Height, 50, 300, ArrayHelper.Null<GUILayoutOption>() ) );
            GUILayout.EndHorizontal();

            GUILayout.BeginHorizontal( ArrayHelper.Null<GUILayoutOption>() );
            GUILayout.Label( "Width", ArrayHelper.Null<GUILayoutOption>() );
            _viewModel.Width = Mathf.Round( GUILayout.HorizontalSlider( _viewModel.Width, 200, 1000, ArrayHelper.Null<GUILayoutOption>() ) );
            GUILayout.EndHorizontal();

            GUI.DragWindow();
         }
         finally
         {
            AutoTranslationPlugin.Current.EnableAutoTranslator();
         }
      }
   }
}
